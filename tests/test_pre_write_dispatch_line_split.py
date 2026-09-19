"""Executed differential harness for writ-pre-write-dispatch.sh's positional
line splits (plan.md "How equivalence is PROVEN, not argued").

This hook decides ALLOW, ASK or DENY on every write. Its stdin parse is split
positionally today by `head`/`sed`/`tail`/`tr`/`basename` (lines 105-107,
206-214, 291), and the plan replaces that with bash builtins (`mapfile -t`,
`${VAR//pattern/}`, `${VAR##*/}`). `mapfile -t` and `sed -n 'Np'` are NOT
equivalent on edge cases, so this file runs BOTH spellings through real
`bash -c` over one corpus of shaped inputs and compares BYTE FOR BYTE,
per field, per input. Nothing here is reasoned about; every claim below was
executed against this machine's bash and coreutils before being written down.

THE OLD SPELLING is hardcoded (OLD_CHUNK1_SPLIT, OLD_CHUNK2_SPLIT) rather than
extracted from the hook, because it is being RETIRED, not kept as a permanent
coexisting fallback arm (unlike the jq/python parse arms `_python_reference()`
in test_pre_write_parse_parity.py pulls apart, which both live in the hook
forever). Once the builtin split lands, the old spelling has nowhere to be
extracted FROM any more. Both literals below were read verbatim out of
hooks/scripts/writ-pre-write-dispatch.sh while authoring this file (verified
byte-for-byte against the real source, not retyped from a paraphrase).

THE NEW SPELLING does not exist yet. It is extracted from the hook (following
the extraction discipline the plan asks for) between two stable text anchors
that survive the conversion either way. Right now that extraction finds the
OLD spelling still in place and calls `pytest.fail("skeleton: ...")` --
loudly, never a skip -- because the capability is not built. Once the
implementation lands a `mapfile`-based replacement between those same
anchors, the same extraction call starts running it for real and the
differential comparison against the (fixed) old baseline becomes the actual
proof.

TRAPS COVERED (from the dispatch brief), each with its own named corpus case
so a failure says which one broke:

  1. "line 3 onward", not line 3: CHUNK1_CASES["check_body_line_three_onward_four_lines"]
     and CHUNK1_CASES["check_body_truncation_json_still_parses"] --
     TestChunk1ParsedInputSplit.test_four_line_input_joins_lines_three_and_four_not_line_three_alone
     and .test_check_body_truncation_changes_curl_failure_mode_not_just_bytes
  2. $() strips the trailing newline, so an empty final field yields SIX
     lines not seven: CHUNK2_CASES["dispatch_blob_six_lines_empty_trailing_mode"] --
     TestChunk2DispatchBlobSplit.test_six_line_dispatch_blob_mode_reads_empty_not_unset
     and .test_unset_array_index_needs_an_explicit_default_to_read_as_empty
  3. an empty field in the MIDDLE: CHUNK1_CASES["empty_field_in_the_middle"],
     CHUNK2_CASES["empty_field_in_the_middle"]
  4. fewer / more lines than expected: CHUNK1_CASES["fewer_lines_than_expected_two_lines"]
     / ["more_lines_than_expected_five_lines"], CHUNK2_CASES["fewer_lines_than_expected_five_lines"]
     / ["more_lines_than_expected_eight_lines"]
  5. whitespace -- leading/trailing spaces and an embedded tab, as SEPARATE
     cases: CHUNK1_CASES["leading_and_trailing_spaces"] / ["embedded_tab"],
     CHUNK2_CASES["leading_and_trailing_spaces_in_a_field"] / ["mode_with_embedded_tab"]
  6. CRLF endings: CHUNK1_CASES["crlf_line_endings"], CHUNK2_CASES["crlf_line_endings"]
  7. shell-special characters, one case per character:
     CHUNK1_CASES["backslash_char"] / ["dollar_sign_char"] / ["backtick_char"]
     / ["single_quote_char"], and the CHUNK2 equivalents

Plus, separately: MODE stripped by `tr -d '[:space:]'` vs `${MODE//[[:space:]]/}`
(TestModeStrippingTrVsExpansion) and the DECISION_FILE display name computed
by `basename` vs `${x##*/}` (TestBasenameVsParameterExpansion) -- both are
general bash-semantics claims, tested directly, independent of where the hook
wires them in, so they are not gated behind the hook-extraction pytest.fail.

Finally, TestStdinReadPayloadSizeSweep and TestStdinReadCorrectnessDifferences
cover the MEASURE-FIRST stdin read at line 31 (`STDIN_DATA=$(cat)` vs
`IFS= read -r -d ''`), with a stated abort criterion in the sweep's docstring.

Run: .venv/bin/python -m pytest tests/test_pre_write_dispatch_line_split.py -v
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-pre-write-dispatch.sh"

TIMEOUT = 30


def setup_module(module) -> None:  # noqa: ANN001
    assert HOOK.exists(), f"hook not found at {HOOK}"


# ---------------------------------------------------------------------------
# Generic field-capture runner: run a bash fragment with one payload variable
# bound, then print the named result variables NUL-separated so python gets
# back exact bytes with no shell-imposed re-quoting.
# ---------------------------------------------------------------------------

def _run_field_capture(preamble: str, payload_var: str, payload_value: str,
                        field_names: tuple[str, ...]) -> tuple[bytes, ...]:
    fmt = "\\0".join(["%s"] * len(field_names))
    args = " ".join(f'"${{{n}:-}}"' for n in field_names)
    script = f"{preamble}\nprintf '{fmt}' {args}"
    env = dict(os.environ)
    env[payload_var] = payload_value
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, env=env, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, (
        f"bash fragment exited {proc.returncode}\nstderr: {proc.stderr!r}\n"
        f"script:\n{script}"
    )
    parts = tuple(proc.stdout.split(b"\x00"))
    assert len(parts) == len(field_names), (
        f"expected {len(field_names)} NUL-separated fields, got {len(parts)}: {parts!r}"
    )
    return parts


def _extract_between(src: str, start_marker: str, end_marker: str, *, what: str) -> str:
    start = src.find(start_marker)
    if start == -1:
        pytest.fail(f"skeleton: could not find the start anchor for {what} in {HOOK}: {start_marker!r}")
    start += len(start_marker)
    end = src.find(end_marker, start)
    if end == -1:
        pytest.fail(f"skeleton: could not find the end anchor for {what} in {HOOK}: {end_marker!r}")
    return src[start:end]


# ---------------------------------------------------------------------------
# Chunk 1: PARSED_INPUT -> SESSION_ID / WRITE_CTX / CHECK_BODY (hook lines 105-107)
# ---------------------------------------------------------------------------

CHUNK1_FIELDS = ("SESSION_ID", "WRITE_CTX", "CHECK_BODY")

# Read verbatim from hooks/scripts/writ-pre-write-dispatch.sh:105-107 while
# authoring this file. Hardcoded (not extracted) because this spelling is
# being retired, not kept as a coexisting fallback arm -- see module docstring.
OLD_CHUNK1_SPLIT = (
    'SESSION_ID=$(echo "$PARSED_INPUT" | head -1)\n'
    'WRITE_CTX=$(echo "$PARSED_INPUT" | sed -n 2p)\n'
    'CHECK_BODY=$(echo "$PARSED_INPUT" | tail -n +3)\n'
)

_CHUNK1_START = '"$STDIN_DATA" "$SKILL_DIR" 2>/dev/null)'
_CHUNK1_END = "# NO SYNTHESIZED ID"


@functools.lru_cache(maxsize=None)
def _run_old_chunk1(parsed_input: str) -> tuple[bytes, ...]:
    return _run_field_capture(OLD_CHUNK1_SPLIT, "PARSED_INPUT", parsed_input, CHUNK1_FIELDS)


def _new_chunk1_block() -> str:
    block = _extract_between(HOOK.read_text(), _CHUNK1_START, _CHUNK1_END, what="the PARSED_INPUT split")
    if "head -1" in block or "sed -n 2p" in block or "tail -n +3" in block:
        pytest.fail(
            "skeleton: writ-pre-write-dispatch.sh still splits PARSED_INPUT into "
            "SESSION_ID / WRITE_CTX / CHECK_BODY with head/sed/tail; the "
            "mapfile-based builtin split this cycle adds has not been "
            f"implemented yet. Region found between the two anchors:\n{block}"
        )
    if "mapfile" not in block:
        pytest.fail(
            "skeleton: expected a `mapfile -t` builtin split for SESSION_ID / "
            "WRITE_CTX / CHECK_BODY in writ-pre-write-dispatch.sh; found "
            "neither the retired head/sed/tail spelling nor a mapfile "
            f"replacement. Region was:\n{block}"
        )
    return block


@functools.lru_cache(maxsize=None)
def _run_new_chunk1(parsed_input: str) -> tuple[bytes, ...]:
    return _run_field_capture(_new_chunk1_block(), "PARSED_INPUT", parsed_input, CHUNK1_FIELDS)


CHUNK1_CASES: dict[str, str] = {
    "trailing_newline_absent": 'sid-1\nctx-1\n{"a": 1}',
    "trailing_newline_present": 'sid-1\nctx-1\n{"a": 1}\n',
    "check_body_line_three_onward_four_lines": "sid\nctx\nline3\nline4",
    "check_body_truncation_json_still_parses":
        'sid\nctx\n{"file_path": "a"}\n{"trailing": "stray"}',
    "empty_field_in_the_middle": 'sid\n\n{"a": 1}',
    "fewer_lines_than_expected_two_lines": "sid\nctx",
    "more_lines_than_expected_five_lines": "sid\nctx\nline3\nline4\nline5",
    "leading_and_trailing_spaces": 'sid\n  ctx with spaces  \n{"a": 1}',
    "embedded_tab": 'sid\nctx\twith\ttab\n{"a": 1}',
    "whitespace_only_field": 'sid\n   \n{"a": 1}',
    "crlf_line_endings": "sid\r\nctx line\r\nbody line1\r\nbody line2",
    "completely_empty_string": "",
    "single_line_no_newline": "sid-only",
    "backslash_char": 'sid\nctx has \\ backslash\n{"a": 1}',
    "dollar_sign_char": 'sid\nctx has $VAR dollar\n{"a": 1}',
    "backtick_char": 'sid\nctx has `cmd` backtick\n{"a": 1}',
    "single_quote_char": 'sid\nctx has \'quoted\' text\n{"a": 1}',
}

_CHUNK1_FIELD_CASES = [
    (name, idx, field) for name in sorted(CHUNK1_CASES) for idx, field in enumerate(CHUNK1_FIELDS)
]


class TestChunk1ParsedInputSplit:
    """SESSION_ID / WRITE_CTX / CHECK_BODY, hook lines 105-107."""

    @pytest.mark.parametrize(
        "name,field_idx,field_name", _CHUNK1_FIELD_CASES,
        ids=[f"{n}::{f}" for n, _, f in _CHUNK1_FIELD_CASES],
    )
    def test_field_is_byte_identical_to_old_split(self, name, field_idx, field_name) -> None:
        raw = CHUNK1_CASES[name]
        old = _run_old_chunk1(raw)
        new = _run_new_chunk1(raw)
        assert new[field_idx] == old[field_idx], (
            f"{name}: {field_name} differs -- old={old[field_idx]!r} new={new[field_idx]!r}"
        )

    def test_four_line_input_joins_lines_three_and_four_not_line_three_alone(self) -> None:
        """Trap 1: `tail -n +3` means line 3 ONWARD. Pins the "correct" value
        by execution, then proves a naive single-index replacement would have
        truncated it, so the two are not accidentally identical on this input.
        """
        raw = CHUNK1_CASES["check_body_line_three_onward_four_lines"]
        old = _run_old_chunk1(raw)
        check_body = old[CHUNK1_FIELDS.index("CHECK_BODY")]
        assert check_body == b"line3\nline4", (
            f"the tail -n +3 contract changed on this input: got {check_body!r}"
        )
        naive = _run_field_capture(
            'mapfile -t _NAIVE <<<"$PARSED_INPUT"\nCHECK_BODY="${_NAIVE[2]:-}"\n',
            "PARSED_INPUT", raw, ("CHECK_BODY",),
        )[0]
        assert naive == b"line3", (
            f"sanity: the naive ${{ARR[2]}} replacement should truncate to line 3 alone, got {naive!r}"
        )
        assert naive != check_body, (
            "the naive ${ARR[2]} replacement and the correct tail -n +3 semantics "
            "coincidentally agree on this input; this corpus case needs different content"
        )
        new = _run_new_chunk1(raw)
        assert new[CHUNK1_FIELDS.index("CHECK_BODY")] == check_body, (
            "the builtin splitter must reproduce tail -n +3's four-line join, not the naive index"
        )

    def test_check_body_truncation_changes_curl_failure_mode_not_just_bytes(self) -> None:
        """Trap 1's sharp edge: not just different bytes, different FAILURE
        MODES. The tail -n +3 spelling produces a body the server rejects
        (curl fails, RESULT empty, the gate is SKIPPED); the naive ${ARR[2]}
        spelling produces a body that STILL PARSES, so the gate runs on a
        truncated request. Both are unacceptable; this pins that the builtin
        splitter reproduces the tail -n +3 failure mode, never the naive one.
        """
        raw = CHUNK1_CASES["check_body_truncation_json_still_parses"]
        old = _run_old_chunk1(raw)
        old_body = old[CHUNK1_FIELDS.index("CHECK_BODY")].decode()
        with pytest.raises(json.JSONDecodeError):
            json.loads(old_body)

        naive = _run_field_capture(
            'mapfile -t _NAIVE <<<"$PARSED_INPUT"\nCHECK_BODY="${_NAIVE[2]:-}"\n',
            "PARSED_INPUT", raw, ("CHECK_BODY",),
        )[0].decode()
        parsed = json.loads(naive)  # must NOT raise: this is the dangerous case
        assert parsed == {"file_path": "a"}, f"naive body parsed to an unexpected shape: {parsed!r}"

        new = _run_new_chunk1(raw)
        new_body = new[CHUNK1_FIELDS.index("CHECK_BODY")]
        assert new_body == old[CHUNK1_FIELDS.index("CHECK_BODY")], (
            "the builtin splitter must reproduce tail -n +3's rejected body, "
            "not the naive single-index truncation that would still parse"
        )


# ---------------------------------------------------------------------------
# Chunk 2: DISPATCH_BLOB -> 7 fields incl. MODE tr-stripped (hook lines 206-214)
# ---------------------------------------------------------------------------

CHUNK2_FIELDS = (
    "DECISION", "DECISION_FILE", "HOOK_OUTPUT", "RAG_RULES_RAW", "NEW_RULE_IDS", "COST", "MODE",
)

# Read verbatim from hooks/scripts/writ-pre-write-dispatch.sh:206-214.
OLD_CHUNK2_SPLIT = (
    "DECISION=$(echo \"$DISPATCH_BLOB\" | sed -n '1p')\n"
    "DECISION_FILE=$(echo \"$DISPATCH_BLOB\" | sed -n '2p')\n"
    "HOOK_OUTPUT=$(echo \"$DISPATCH_BLOB\" | sed -n '3p')\n"
    "RAG_RULES_RAW=$(echo \"$DISPATCH_BLOB\" | sed -n '4p')\n"
    "NEW_RULE_IDS=$(echo \"$DISPATCH_BLOB\" | sed -n '5p')\n"
    "COST=$(echo \"$DISPATCH_BLOB\" | sed -n '6p')\n"
    "MODE=$(echo \"$DISPATCH_BLOB\" | sed -n '7p' | tr -d '[:space:]')\n"
)

# ANCHOR ROT, FIXED RATHER THAN WORKED AROUND. Both anchors used to be the two strings
# plan dfacff61-23d5-474e-846c-2e2f0f0ea482 deleted: the translator's argv
# (`"$RESULT" "$CHECK_BODY" 2>/dev/null || echo "")`, now two NUL-separated records on its
# stdin) and the silent-allow default (`DECISION="${DECISION:-allow}"`, now an
# observed-outcome branch). The REGION this module measures, the mapfile split of the seven
# DISPATCH_BLOB fields, is untouched by that cycle; only the fences around it moved. The
# start anchor is the translator program's last line plus the substitution's closing form,
# which is unique in the file (`" 2>/dev/null || echo "")` alone is not: the `RESULT=`
# assignment above matches it too, and `str.find` would have taken that one and swallowed
# the whole python program into the "split block").
_CHUNK2_START = 'sys.stdout.write(mode + \'\\n\')\n" 2>/dev/null || echo "")'
_CHUNK2_END = 'if [ -z "$DECISION" ]; then'


@functools.lru_cache(maxsize=None)
def _run_old_chunk2(dispatch_blob: str) -> tuple[bytes, ...]:
    return _run_field_capture(OLD_CHUNK2_SPLIT, "DISPATCH_BLOB", dispatch_blob, CHUNK2_FIELDS)


def _new_chunk2_block() -> str:
    block = _extract_between(HOOK.read_text(), _CHUNK2_START, _CHUNK2_END, what="the DISPATCH_BLOB split")
    if "sed -n" in block or "tr -d" in block:
        pytest.fail(
            "skeleton: writ-pre-write-dispatch.sh still splits DISPATCH_BLOB into "
            "its 7 fields with sed, and strips MODE with tr; the mapfile-based "
            "builtin split this cycle adds has not been implemented yet. Region "
            f"found between the two anchors:\n{block}"
        )
    if "mapfile" not in block:
        pytest.fail(
            "skeleton: expected a `mapfile -t` builtin split for the 7 "
            "DISPATCH_BLOB fields in writ-pre-write-dispatch.sh; found neither "
            f"the retired sed/tr spelling nor a mapfile replacement. Region was:\n{block}"
        )
    return block


@functools.lru_cache(maxsize=None)
def _run_new_chunk2(dispatch_blob: str) -> tuple[bytes, ...]:
    return _run_field_capture(_new_chunk2_block(), "DISPATCH_BLOB", dispatch_blob, CHUNK2_FIELDS)


CHUNK2_CASES: dict[str, str] = {
    "normal_seven_fields": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0\nwork",
    "dispatch_blob_six_lines_empty_trailing_mode": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0",
    "empty_field_in_the_middle": "allow\n\nHOOKOUT\nRAG\n[]\n0\nwork",
    "fewer_lines_than_expected_five_lines": "allow\nfile.py\nHOOKOUT\nRAG\n[]",
    "more_lines_than_expected_eight_lines": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0\nwork\nEXTRA",
    "whitespace_only_mode": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0\n   ",
    "mode_with_embedded_tab": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0\nwor\tk",
    "leading_and_trailing_spaces_in_a_field": "allow\n  file.py  \nHOOKOUT\nRAG\n[]\n0\nwork",
    "trailing_newline_present": "allow\nfile.py\nHOOKOUT\nRAG\n[]\n0\nwork\n",
    "crlf_line_endings": "allow\r\nfile.py\r\nHOOKOUT\r\nRAG\r\n[]\r\n0\r\nwork",
    "completely_empty_string": "",
    "backslash_char": "allow\nfile.py\nHOOK\\OUT\nRAG\n[]\n0\nwork",
    "dollar_sign_char": "allow\nfile.py\nHOOK$OUT\nRAG\n[]\n0\nwork",
    "backtick_char": "allow\nfile.py\nHOOK`OUT`\nRAG\n[]\n0\nwork",
    "single_quote_char": "allow\nfile.py\nHOOK'OUT'\nRAG\n[]\n0\nwork",
}

_CHUNK2_FIELD_CASES = [
    (name, idx, field) for name in sorted(CHUNK2_CASES) for idx, field in enumerate(CHUNK2_FIELDS)
]


class TestChunk2DispatchBlobSplit:
    """DECISION/DECISION_FILE/HOOK_OUTPUT/RAG_RULES_RAW/NEW_RULE_IDS/COST/MODE,
    hook lines 206-214."""

    @pytest.mark.parametrize(
        "name,field_idx,field_name", _CHUNK2_FIELD_CASES,
        ids=[f"{n}::{f}" for n, _, f in _CHUNK2_FIELD_CASES],
    )
    def test_field_is_byte_identical_to_old_split(self, name, field_idx, field_name) -> None:
        raw = CHUNK2_CASES[name]
        old = _run_old_chunk2(raw)
        new = _run_new_chunk2(raw)
        assert new[field_idx] == old[field_idx], (
            f"{name}: {field_name} differs -- old={old[field_idx]!r} new={new[field_idx]!r}"
        )

    def test_six_line_dispatch_blob_mode_reads_empty_not_unset(self) -> None:
        """Trap 2: $() strips ALL trailing newlines, so a run whose final field
        (mode) is empty produces SIX lines, not seven -- the normal path, not
        an exotic one. `sed -n '7p'` on six lines prints nothing (empty);
        `${ARR[6]}` is unset unless read with an explicit `:-` default.
        """
        raw = CHUNK2_CASES["dispatch_blob_six_lines_empty_trailing_mode"]
        length = _run_field_capture(
            'mapfile -t _A <<<"$DISPATCH_BLOB"\nLEN="${#_A[@]}"\n',
            "DISPATCH_BLOB", raw, ("LEN",),
        )[0]
        assert length == b"6", f"expected the trailing-mode-empty case to yield 6 lines, got {length!r}"

        old = _run_old_chunk2(raw)
        assert old[CHUNK2_FIELDS.index("MODE")] == b"", "OLD: mode should read as empty on 6 lines"

        new = _run_new_chunk2(raw)
        assert new[CHUNK2_FIELDS.index("MODE")] == b"", "NEW: mode should read as empty, not unset"

    def test_unset_array_index_needs_an_explicit_default_to_read_as_empty(self) -> None:
        """Documents WHY the builtin splitter needs `${ARR[6]:-}`, not bare
        `${ARR[6]}`. The real hook does not `set -u` today (verified: no
        `set -euo pipefail` anywhere in the file), so this is a should-not-
        regress guard on the CONTRACT the replacement must honor if a future
        caller ever does enable it, not a claim about the hook's current
        settings.
        """
        raw = CHUNK2_CASES["dispatch_blob_six_lines_empty_trailing_mode"]
        env = {**os.environ, "DISPATCH_BLOB": raw}
        no_default = subprocess.run(
            ["bash", "-c", 'set -u; mapfile -t _A <<<"$DISPATCH_BLOB"; echo "${_A[6]}"'],
            capture_output=True, env=env, timeout=TIMEOUT,
        )
        assert no_default.returncode != 0, "expected set -u to reject the unset index without a default"
        assert b"unbound variable" in no_default.stderr

        with_default = subprocess.run(
            ["bash", "-c", 'set -u; mapfile -t _A <<<"$DISPATCH_BLOB"; echo "${_A[6]:-}"'],
            capture_output=True, env=env, timeout=TIMEOUT,
        )
        assert with_default.returncode == 0, with_default.stderr
        assert with_default.stdout.strip() == b""


# ---------------------------------------------------------------------------
# Anti-vacuity: the corpus itself, and each splitter's output, must not be
# trivially empty everywhere -- a differential test where both sides always
# produce "" passes for free and proves nothing.
# ---------------------------------------------------------------------------

class TestCorpusIsNotVacuous:
    def test_chunk1_corpus_is_non_empty(self) -> None:
        assert len(CHUNK1_CASES) > 0

    def test_chunk2_corpus_is_non_empty(self) -> None:
        assert len(CHUNK2_CASES) > 0

    @pytest.mark.parametrize("name", sorted(set(CHUNK1_CASES) - {"completely_empty_string"}))
    def test_chunk1_old_split_has_at_least_one_non_empty_field(self, name) -> None:
        old = _run_old_chunk1(CHUNK1_CASES[name])
        assert any(old), f"{name}: every field was empty on the OLD split; this case is vacuous"

    @pytest.mark.parametrize("name", sorted(set(CHUNK2_CASES) - {"completely_empty_string"}))
    def test_chunk2_old_split_has_at_least_one_non_empty_field(self, name) -> None:
        old = _run_old_chunk2(CHUNK2_CASES[name])
        assert any(old), f"{name}: every field was empty on the OLD split; this case is vacuous"

    def test_completely_empty_string_case_is_all_empty_on_purpose(self) -> None:
        """The one deliberate exception to the rule above: the hook reaches
        this state whenever the python/jq parse fails and `|| echo ""`
        supplies the empty default, so it must be in the corpus even though
        every field is empty by design.
        """
        old1 = _run_old_chunk1(CHUNK1_CASES["completely_empty_string"])
        assert old1 == (b"", b"", b"")
        old2 = _run_old_chunk2(CHUNK2_CASES["completely_empty_string"])
        assert all(f == b"" for f in old2)


# ---------------------------------------------------------------------------
# MODE stripping: tr -d '[:space:]' vs ${MODE//[[:space:]]/} -- a general bash
# claim, tested directly (not gated behind hook extraction).
# ---------------------------------------------------------------------------

MODE_CASES: dict[str, str] = {
    "empty": "",
    "whitespace_only_spaces": "   ",
    "whitespace_only_tabs_and_spaces": "\t \t",
    "ordinary": "work",
    "embedded_tab": "wor\tk",
    "non_ascii_word": "wörk",
    "non_ascii_only": "é",
}


def _run_tr_strip(value: str) -> bytes:
    proc = subprocess.run(
        ["bash", "-c", 'printf "%s" "$1" | tr -d "[:space:]"', "tr-probe", value],
        capture_output=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _run_expansion_strip(value: str) -> bytes:
    proc = subprocess.run(
        ["bash", "-c", 'MODE="$1"; printf "%s" "${MODE//[[:space:]]/}"', "expansion-strip-probe", value],
        capture_output=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TestModeStrippingTrVsExpansion:
    def test_corpus_is_non_empty(self) -> None:
        assert len(MODE_CASES) > 0

    @pytest.mark.parametrize("name", sorted(MODE_CASES))
    def test_tr_and_expansion_agree_or_the_divergence_is_recorded(self, name) -> None:
        """Plan claim: these agree on ASCII and the mode is always a short
        lowercase word, but under a multibyte locale `tr` CAN mangle a
        non-ASCII byte sequence where bash's pattern matching will not.
        Measured on this machine (UTF-8 and C locale both): they agree on
        every corpus case. If a future environment disagrees, the bash
        expansion result is authoritative (it is what the converted hook
        would use), and this failure exists to surface that, not hide it.
        """
        value = MODE_CASES[name]
        tr_out = _run_tr_strip(value)
        exp_out = _run_expansion_strip(value)
        assert tr_out == exp_out, (
            f"{name}: tr={tr_out!r} bash-expansion={exp_out!r} disagree; per the "
            f"plan the bash-expansion result is authoritative for the converted hook"
        )

    def test_ordinary_mode_strips_to_the_same_non_empty_word(self) -> None:
        tr_out = _run_tr_strip(MODE_CASES["ordinary"])
        assert tr_out == b"work"


# ---------------------------------------------------------------------------
# basename vs ${x##*/}: general bash claim, tested directly.
# ---------------------------------------------------------------------------

BASENAME_CASES: dict[str, str] = {
    "ordinary_path": "/skill/dir/rules/foo.md",
    "no_slash": "foo.md",
    "dot": ".",
    "unicode_name": "/skill/dir/café.md",
    "trailing_slash": "/a/b/",
    "double_trailing_slash": "/a/b//",
    "bare_root_slash": "/",
    "leading_dash": "-rf",
}

_AGREE_CASES = ("ordinary_path", "no_slash", "dot", "unicode_name")
_DIFFER_CASES = ("trailing_slash", "double_trailing_slash", "bare_root_slash", "leading_dash")

# Recorded values, pinned by execution (see the differing outputs measured
# while authoring this file). The real hook calls `basename "${DECISION_FILE:-unknown}"`
# with NO `--` separator, so `leading_dash` reproduces that exact call shape:
# GNU basename treats "-rf" as an unknown option and exits 1 with empty stdout.
_EXPECTED_DIFFERENCES: dict[str, tuple[int, bytes, bytes]] = {
    "trailing_slash": (0, b"b", b""),
    "double_trailing_slash": (0, b"b", b""),
    "bare_root_slash": (0, b"/", b""),
    "leading_dash": (1, b"", b"-rf"),
}


def _run_basename(value: str) -> tuple[int, bytes]:
    proc = subprocess.run(
        ["bash", "-c", 'basename "$1"', "basename-probe", value],
        capture_output=True, timeout=TIMEOUT,
    )
    return proc.returncode, proc.stdout.rstrip(b"\n")


def _run_expansion_basename(value: str) -> bytes:
    proc = subprocess.run(
        ["bash", "-c", 'printf "%s" "${1##*/}"', "expansion-probe", value],
        capture_output=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TestBasenameVsParameterExpansion:
    def test_corpus_is_non_empty(self) -> None:
        assert len(BASENAME_CASES) > 0

    @pytest.mark.parametrize("name", sorted(_AGREE_CASES))
    def test_agrees_with_basename_on_ordinary_paths(self, name) -> None:
        value = BASENAME_CASES[name]
        rc, out = _run_basename(value)
        assert rc == 0, f"{name}: basename itself failed: {out!r}"
        exp = _run_expansion_basename(value)
        assert out == exp, f"{name}: basename={out!r} expansion={exp!r}"
        assert out != b"", f"{name}: vacuous -- both sides produced empty output"

    @pytest.mark.parametrize("name", sorted(_DIFFER_CASES))
    def test_differs_from_basename_with_recorded_values(self, name) -> None:
        value = BASENAME_CASES[name]
        rc, out = _run_basename(value)
        exp = _run_expansion_basename(value)
        assert out != exp, (
            f"{name}: expected a recorded divergence but basename={out!r} equals "
            f"expansion={exp!r}; the pinned difference has disappeared"
        )
        exp_rc, exp_basename, exp_expansion = _EXPECTED_DIFFERENCES[name]
        assert rc == exp_rc, f"{name}: basename rc={rc}, expected {exp_rc}"
        assert out == exp_basename, f"{name}: basename={out!r}, expected {exp_basename!r}"
        assert exp == exp_expansion, f"{name}: expansion={exp!r}, expected {exp_expansion!r}"


# ---------------------------------------------------------------------------
# Stdin read: STDIN_DATA=$(cat) vs IFS= read -r -d '' (hook line 31).
# MEASURE-FIRST per the plan: this may not be taken at all.
# ---------------------------------------------------------------------------

CAT_READ_SCRIPT = 'X=$(cat); printf "%s" "${#X}"'
BUILTIN_READ_SCRIPT = 'IFS= read -r -d "" X || true; printf "%s" "${#X}"'


def _median_wall_time(script: str, payload: bytes, trials: int = 7) -> float:
    times = []
    for _ in range(trials):
        t0 = time.perf_counter()
        proc = subprocess.run(
            ["bash", "-c", script], input=payload,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
        )
        times.append(time.perf_counter() - t0)
        assert proc.returncode == 0, proc.stderr
    times.sort()
    return times[len(times) // 2]


class TestStdinReadDecision:
    """PINS THE DECISION, not the rejected hypothesis. An earlier version of
    this class asked "is `read` not slower than `cat`" and measured that it
    is, decisively, so that framing can never go green again -- a
    permanently red test trains everyone to ignore a red suite. The MEASURED
    decision is: `STDIN_DATA=$(cat)` stays at
    hooks/scripts/writ-pre-write-dispatch.sh:31 and `IFS= read -r -d ''` is
    not taken. Mechanism, recorded so a future reader does not fall into the
    same trap re-measuring this: bash's `read` builtin reads a SEEKABLE fd in
    large blocks (seeking back after each one), but falls back to unbuffered
    single-byte reads on an unseekable fd such as a pipe, and Claude Code
    always delivers hook stdin as a pipe, never a seekable file, which is why
    a re-measurement via `< file` redirect (14.6 ms for 1 MB) looks nothing
    like the real path (446.0 ms for the same 1 MB over a pipe, roughly 30x
    apart) and must not be used to revisit this decision.
    """

    def test_the_hook_still_reads_stdin_with_cat_not_the_builtin_read(self) -> None:
        """The ratchet: if this goes red, someone swapped $(cat) for the
        builtin read believing it is faster. On the fd shape this hook
        actually receives (a pipe), it is not -- see the class docstring and
        test_read_from_a_pipe_is_at_least_three_times_slower_than_cat_at_1mb
        below. If a future bash release fixes pipe-read performance and this
        is re-measured FROM A PIPE with a different conclusion, update this
        pin deliberately; do not just delete it because it is inconvenient.
        """
        src = HOOK.read_text()
        assert "STDIN_DATA=$(cat)" in src, (
            "writ-pre-write-dispatch.sh no longer reads stdin with $(cat); "
            "this was a deliberate MEASURED decision (see the class docstring)"
        )
        # Checked against CODE lines only, not the whole file: the hook is
        # expected to carry a comment recording the rejected `IFS= read -r -d
        # ''` alternative (that is exactly the decision this test pins), so a
        # raw substring search over the full source would trip on its own
        # explanation. A real adoption would show up as one of these two
        # spellings on a line that is not a comment.
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        offenders = [
            ln for ln in code_lines
            if "IFS= read -r -d ''" in ln or 'IFS= read -r -d ""' in ln
        ]
        assert offenders == [], (
            f"writ-pre-write-dispatch.sh has adopted the builtin stdin read as "
            f"code: {offenders!r}; the measured decision was to keep $(cat) instead"
        )

    def test_read_from_a_pipe_is_at_least_three_times_slower_than_cat_at_1mb(self) -> None:
        """The REASON the decision stands, at a generous floor rather than a
        tight ratio: the measured margin was 10x-64x across repeated runs, so
        3x is robust to machine variance while still going red if bash's
        pipe-read behavior is ever fixed upstream -- exactly when this
        decision should be revisited. Piped via `subprocess.run(input=...)`,
        never a file redirect: that distinction is the entire finding.
        """
        payload = b"x" * (1024 * 1024)
        cat_t = _median_wall_time(CAT_READ_SCRIPT, payload)
        read_t = _median_wall_time(BUILTIN_READ_SCRIPT, payload)
        ratio = read_t / cat_t if cat_t > 0 else float("inf")
        print(f"pipe read 1MB: cat={cat_t * 1000:.2f}ms read={read_t * 1000:.2f}ms ratio={ratio:.2f}x")
        assert ratio >= 3.0, (
            f"expected read-from-a-pipe to be at least 3x slower than $(cat) at "
            f"1 MB (measured margin across runs was 10x-64x); got {ratio:.2f}x "
            f"(read={read_t * 1000:.2f}ms, cat={cat_t * 1000:.2f}ms). If bash's "
            f"pipe read has genuinely improved, the $(cat) decision should be "
            f"revisited, not this floor quietly raised."
        )


class TestStdinReadCorrectnessDifferences:
    """Byte-level behavior differences, confirmed by direct execution, that
    hold regardless of which way the timing sweep above comes out."""

    def _capture(self, script: str, payload: bytes) -> bytes:
        proc = subprocess.run(
            ["bash", "-c", script], input=payload, capture_output=True, timeout=TIMEOUT,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def test_cat_strips_a_trailing_newline_the_builtin_read_preserves(self) -> None:
        payload = b"hello\n"
        cat_out = self._capture('X=$(cat); printf "%s" "$X"', payload)
        read_out = self._capture('IFS= read -r -d "" X || true; printf "%s" "$X"', payload)
        assert cat_out == b"hello", f"$(cat) unexpectedly kept the newline: {cat_out!r}"
        assert read_out == b"hello\n", f"read unexpectedly dropped the newline: {read_out!r}"
        assert cat_out != read_out, (
            "if this ever holds, $(cat) and the builtin read have converged on "
            "trailing-newline handling and the write-path body construction "
            "code should be re-audited for the assumption this test pins"
        )

    def test_cat_drops_an_embedded_nul_and_keeps_what_follows(self) -> None:
        payload = b"hello\x00world"
        cat_out = self._capture('X=$(cat); printf "%s" "$X"', payload)
        assert cat_out == b"helloworld", f"$(cat) NUL handling changed: {cat_out!r}"

    def test_builtin_read_truncates_at_the_first_nul(self) -> None:
        payload = b"hello\x00world"
        read_out = self._capture('IFS= read -r -d "" X || true; printf "%s" "$X"', payload)
        assert read_out == b"hello", f"read NUL handling changed: {read_out!r}"
        assert b"world" not in read_out, (
            "read must truncate at the first NUL, not just reorder or partially keep the tail"
        )
