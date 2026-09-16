"""The prompt-parse record's field frame: the only field that may contain the delimiter
must be last, and the hook must read it as the remainder.

THE DEFECT, restated as field math. `bin/lib/writ-prompt-parse.py` prints five
newline-separated fields with the PROMPT second, and its only consumer,
`hooks/scripts/writ-rag-inject.sh`, takes field k from line k. A prompt containing N
newlines therefore lands every later field N lines late: AGENT_ID becomes prompt line 2,
MODE_HINT becomes prompt line 3 with its whitespace stripped, EFFORT becomes prompt line 4,
and the PROMPT itself is truncated to its first line.

THE PRECONDITION THAT BOUNDS IT, measured rather than assumed, and every fixture in this
module respects it: `writ-prompt-parse.py:75` flattens any prompt over 300 characters
through `extract_keywords` BEFORE printing, so a long multi-line prompt arrives as one line
and its frame is intact. Only a prompt that is both multi-line AND at most 300 characters
shifts anything. A fixture that misses that bound reproduces nothing, which is why
`LONG_MULTILINE_PROMPT` is carried here as the regression case (green today, and the thing a
reorder could break) rather than as another instance of the defect.

NOTHING HERE MODELS EITHER SIDE. The repo's parity keystone is that a guard which
hand-rolls one of the two paths it compares proves only that the code matches a model of
the route, and the real route stayed wrong for months. So:

  * the record is produced by executing the REAL parser on a real envelope, and
  * the fields are read back by executing the REAL hook's own assignment bodies, lifted
    verbatim out of the hook by `tests/_inventory.py::rag_inject_field_slices`, and
  * the end-to-end cases run the REAL hook, and read their result out of the session cache
    the REAL session helper wrote, or out of the hook's own debug breadcrumb.

THE ANCHOR LIVES HERE, not beside the derivations in `tests/_inventory.py`. `FIELD_CONTRACT`
is the one human-authored statement of the frame, and it is deliberately in a different file
from the two derivations it judges, so a careless edit cannot move the anchor to agree with
a broken derivation in the same diff.

NO COUNT LITERAL. The field count is `len(FIELD_CONTRACT)` everywhere it is needed,
including the expected `N,$p` tail selector and the malformed arm's empty-field output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PARSE_PY = REPO / "bin" / "lib" / "writ-prompt-parse.py"
HOOK = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
SESSION_HELPER = REPO / "bin" / "lib" / "writ-session.py"

# ─────────────────────────────────────────────────────────────────────────────
# The contract. Shell variable name -> the parser variable that fills it, in the
# order the record is printed and read. The payload field is LAST because it is the
# only one that may contain the delimiter.
# ─────────────────────────────────────────────────────────────────────────────
FIELD_CONTRACT: dict[str, str] = {
    "SESSION_ID": "sid",
    "AGENT_ID": "agent_id",
    "MODE_HINT": "hint",
    "EFFORT": "effort",
    "PROMPT": "prompt",
}
PAYLOAD_FIELD = "PROMPT"
PAYLOAD_POSITION = list(FIELD_CONTRACT).index(PAYLOAD_FIELD) + 1
TAIL_SELECTOR = f"{PAYLOAD_POSITION},$p"

# The spawn budget, as a membership predicate over the commands the extraction block may
# spend, NOT as a count: this is the per-prompt hot path and the whole cost argument for the
# reorder is that it adds no process. `echo` is a bash builtin and spends nothing.
ALLOWED_COMMANDS = {"head", "sed", "tr"}
SHELL_BUILTINS = {"echo"}

# The parametrize population's stand-in when the hook derivation is not there yet. An empty
# `argvalues` list makes pytest report a SKIP, which reads exactly like coverage that ran;
# one sentinel param reads as the RED it is.
_MISSING = "<rag_inject_field_slices() is unavailable>"

EFFORT_LEVEL = "xhigh"

SINGLE_LINE_PROMPT = "hello there world"
SHORT_MULTILINE_PROMPT = (
    "here is the context\nimplement the export endpoint from the approved plan"
)
IMPERSONATING_PROMPT = "agent-xyz\ninvestigate\nxhigh"
EMPTY_PROMPT = ""

# Over 300 characters AND multi-line: `extract_keywords` flattens it before printing, so the
# frame stays whole today. This is the case a careless reorder breaks, not another instance
# of the defect. Its length is asserted in the test rather than trusted.
LONG_MULTILINE_PROMPT = (
    "please review the following implementation plan and then implement it carefully, "
    "taking care to preserve the existing behaviour of the parser and of the hook that "
    "consumes it, because the field frame is positional and a newline in the wrong place "
    "shifts every field after it by one line\n"
    "1. reorder the printed record\n"
    "2. reslice the hook assignments\n"
    "3. add the regression tests that prove it\n"
)

ROUND_TRIP_PROMPTS = [
    pytest.param(SINGLE_LINE_PROMPT, id="single-line"),
    pytest.param(SHORT_MULTILINE_PROMPT, id="short-multi-line"),
    pytest.param(IMPERSONATING_PROMPT, id="lines-shaped-like-field-values"),
    pytest.param(EMPTY_PROMPT, id="empty"),
]

_BREADCRUMB = re.compile(r"session=(?P<sid>.*) prompt_len=(?P<length>\d+)\s*$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _inventory():
    """`tests/_inventory.py` with this cycle's two derivations, or a loud failure."""
    import tests._inventory as inventory

    missing = [
        name
        for name in ("prompt_parse_field_order", "rag_inject_field_slices")
        if not hasattr(inventory, name)
    ]
    if missing:
        pytest.fail(
            f"skeleton: tests/_inventory.py has no {', '.join(missing)} yet (plan.md "
            "## Files assigns the parser's field order and the hook's field slices there, "
            "so the parity check compares two real artifacts instead of a model of either)",
            pytrace=False,
        )
    return inventory


def _slice_names() -> list:
    """Collection-time population for the per-field parametrized cases."""
    try:
        names = sorted(_inventory().rag_inject_field_slices())
    except Exception:  # noqa: BLE001
        return [pytest.param(_MISSING, id="rag-inject-slices-unavailable")]
    return names or [pytest.param(_MISSING, id="rag-inject-slices-empty")]


def _parse(envelope: dict, *, parser: Path = PARSE_PY) -> subprocess.CompletedProcess:
    """The REAL parser, on a real envelope."""
    return subprocess.run(
        [sys.executable, str(parser)],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _slice_through_the_hook(parsed_stdout: str, *, hook: Path = HOOK) -> dict[str, str]:
    """Read the parser's output back with the hook's OWN assignment bodies, verbatim.

    This is the half of the parity that cannot be hand-rolled. The bodies come out of the
    real hook text; they run in a real bash with the real `$PARSED`; the values come back
    NUL-separated because the payload field may legitimately contain newlines.

    `set -euo pipefail` mirrors the hook's own shell options, so an extraction that only
    works without them fails here rather than in production.
    """
    slices = _inventory().rag_inject_field_slices(path=hook)
    assert slices, (
        f"no field assignments derived from {hook}: every assertion built on this read "
        "would pass on any tree"
    )
    script = "set -euo pipefail\n"
    script += "\n".join(f"{name}=$({body})" for name, body in slices.items())
    script += "\nprintf '%s\\0' " + " ".join(f'"${name}"' for name in slices) + "\n"
    env = {"PARSED": parsed_stdout, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    run = subprocess.run(
        ["bash", "-s"], input=script, capture_output=True, text=True, env=env, timeout=30
    )
    assert run.returncode == 0, f"the hook's own extraction block failed: {run.stderr}"
    values = run.stdout.split("\0")[:-1]
    assert len(values) == len(slices), (
        f"expected one value per assignment, got {len(values)} for {list(slices)}"
    )
    return dict(zip(slices, values))


def _fields(envelope: dict, *, parser: Path = PARSE_PY, hook: Path = HOOK) -> dict[str, str]:
    out = _parse(envelope, parser=parser)
    assert out.returncode == 0, out.stderr
    return _slice_through_the_hook(out.stdout, hook=hook)


def _commands(body: str) -> set[str]:
    """The external commands a command-substitution body spends, builtins excluded."""
    found = set()
    for segment in body.split("|"):
        tokens = segment.strip().split()
        if tokens:
            found.add(tokens[0])
    return found - SHELL_BUILTINS


def _copy(source: Path, tmp_path) -> Path:
    """A writable copy under tmp_path. The real tree is never mutated."""
    destination = tmp_path / source.name
    shutil.copy2(source, destination)
    return destination


def _run_hook(tmp_path, prompt: str, sid: str) -> dict:
    """The REAL hook, end to end, on the recipe test_mode_autoroute.py already uses.

    Dead port plus WRIT_NO_AUTOSTART so no daemon is spawned and the hook takes its
    file-direct fallback; cwd inside tmp_path because `mode set` stamps project_root from
    the process cwd and clearing gate state deletes <project_root>/.claude/gates/*.approved.

    PRE-EXISTING AND INHERITED, NOT INTRODUCED HERE: with an empty AGENT_ID the hook writes
    /tmp/writ-current-session, a hardcoded global path with no env knob. The existing
    end-to-end tests already do this on every run. No assertion here reads that file.
    """
    debug_log = tmp_path / "rag-debug.log"
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(tmp_path)
    env["WRIT_PORT"] = "59997"
    env["WRIT_HOST"] = "localhost"
    env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
    env["WRIT_NO_AUTOSTART"] = "1"
    env["WRIT_DEBUG"] = "1"
    env["WRIT_DEBUG_LOG"] = str(debug_log)
    env["WRIT_HOOK_LOG"] = str(tmp_path / "hooks.log")
    sandbox = tmp_path / "sandbox"
    (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir(exist_ok=True)
    run = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"session_id": sid, "prompt": prompt}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(sandbox),
    )
    mode = subprocess.run(
        [sys.executable, str(SESSION_HELPER), "mode", "get", sid],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    caches = list(tmp_path.glob(f"*{sid}.json"))
    cache = json.loads(caches[0].read_text()) if caches else {}
    return {
        "run": run,
        "mode": mode,
        "cache": cache,
        "breadcrumb": _breadcrumb(debug_log),
    }


def _breadcrumb(debug_log: Path) -> dict:
    """The hook's OWN `session=... prompt_len=${#PROMPT}` line (writ-rag-inject.sh:140).

    An INDEPENDENT oracle for prompt wholeness: it is the shell's own expansion of the
    variable the hook actually holds, written by the hook's own debug call under
    WRIT_DEBUG=1, and nothing in this module computes it from the parser's output.
    """
    assert debug_log.exists(), (
        f"the hook wrote no debug log at {debug_log}; WRIT_DEBUG=1 and WRIT_DEBUG_LOG are "
        "the gate on writ-rag-inject.sh:140 and both are set by _run_hook"
    )
    matches = [
        match.groupdict()
        for match in (_BREADCRUMB.search(line) for line in debug_log.read_text().splitlines())
        if match
    ]
    assert matches, (
        f"no `session=... prompt_len=...` breadcrumb in {debug_log}; that line is this "
        "module's independent oracle for prompt wholeness and nothing else can stand in "
        f"for it. Log held:\n{debug_log.read_text()[:2000]}"
    )
    last = matches[-1]
    return {"session_id": last["sid"], "prompt_len": int(last["length"])}


# --------------------------------------------------------------------------- #
# reusable detectors, so the mutation cases below can point them at a copy
# --------------------------------------------------------------------------- #
def _assert_scalars_are_contained(parser: Path) -> None:
    """Capability 6's detector, callable against a mutated copy."""
    session_id = "sid-alpha\nsid-beta"
    agent_id = "agent-one\nagent-two"
    effort = f"{EFFORT_LEVEL}\nEXTRA"
    prompt = "first line\nsecond line"
    out = _parse(
        {
            "session_id": session_id,
            "agent_id": agent_id,
            "prompt": prompt,
            "effort": {"level": effort},
        },
        parser=parser,
    )
    assert out.returncode == 0, out.stderr
    expected_lines = (len(FIELD_CONTRACT) - 1) + len(prompt.split("\n"))
    assert len(out.stdout.split("\n")) - 1 == expected_lines, (
        "a newline inside a scalar moved a field boundary: the record should hold one line "
        f"per scalar plus the prompt's own {len(prompt.split(chr(10)))} line(s); got "
        f"{out.stdout!r}"
    )
    fields = _slice_through_the_hook(out.stdout)
    # sid takes agent_id when the envelope carries one (writ-prompt-parse.py:72), so both
    # fields hold the same sanitized value here.
    assert fields["AGENT_ID"] == "agent-one agent-two"
    assert fields["SESSION_ID"] == "agent-one agent-two"
    # MODE_HINT and EFFORT are read through the hook's own `tr -d '[:space:]'`, so the
    # space the sanitizer leaves behind is stripped again by the consumer.
    assert fields["EFFORT"] == f"{EFFORT_LEVEL}EXTRA"
    assert fields["PROMPT"] == prompt


def _assert_prompt_selector_is_a_tail(hook: Path) -> None:
    """Capability 9's detector, callable against a mutated copy."""
    slices = _inventory().rag_inject_field_slices(path=hook)
    assert PAYLOAD_FIELD in slices, (
        f"the hook has no {PAYLOAD_FIELD} assignment over $PARSED: {sorted(slices)}"
    )
    assert TAIL_SELECTOR in slices[PAYLOAD_FIELD], (
        f"{PAYLOAD_FIELD} must be read as the remainder of the record "
        f"(sed -n '{TAIL_SELECTOR}'), so a prompt containing the delimiter arrives whole; "
        f"got {slices[PAYLOAD_FIELD]!r}"
    )


def _assert_budget(hook: Path) -> None:
    """Capability 10's detector, callable against a mutated copy."""
    slices = _inventory().rag_inject_field_slices(path=hook)
    assert slices, f"no field assignments derived from {hook}"
    spent = set()
    for body in slices.values():
        spent |= _commands(body)
    assert spent <= ALLOWED_COMMANDS, (
        f"the field-extraction block spends {sorted(spent - ALLOWED_COMMANDS)}, outside the "
        f"declared budget {sorted(ALLOWED_COMMANDS)}. This is the per-prompt hot path and "
        "the reorder's whole cost argument is that it adds no process; an interpreter or a "
        "jq start here is a real regression."
    )


def _assert_parser_order_matches_contract(parser: Path) -> None:
    """Capability 8's producer-side detector, callable against a mutated copy."""
    order = _inventory().prompt_parse_field_order(path=parser)
    assert order == list(FIELD_CONTRACT.values()), (
        f"the parser prints {order}, the contract declares "
        f"{list(FIELD_CONTRACT.values())}. The payload field must be last, because it is "
        "the only one that may contain the record's delimiter."
    )


# --------------------------------------------------------------------------- #
# Capability 5, 6: the record the parser really prints, read back by the real hook
# --------------------------------------------------------------------------- #
class TestTheRecordTheParserPrints:

    @pytest.mark.parametrize("prompt", ROUND_TRIP_PROMPTS)
    def test_one_line_per_scalar_and_the_prompt_as_the_remainder(self, prompt) -> None:
        """The frame, for every prompt shape at or under the 300-character bound.

        RED today for the multi-line and field-shaped cases: the prompt is second, so the
        record holds the prompt's lines where AGENT_ID, MODE_HINT and EFFORT should be.
        """
        sid = "sid-frame"
        out = _parse(
            {"session_id": sid, "prompt": prompt, "effort": {"level": EFFORT_LEVEL}}
        )
        assert out.returncode == 0, out.stderr
        expected_lines = (len(FIELD_CONTRACT) - 1) + len(prompt.split("\n"))
        assert len(out.stdout.split("\n")) - 1 == expected_lines, (
            f"expected {len(FIELD_CONTRACT) - 1} scalar line(s) plus the prompt's "
            f"{len(prompt.split(chr(10)))}; got {out.stdout!r}"
        )

    @pytest.mark.parametrize("prompt", ROUND_TRIP_PROMPTS)
    def test_the_prompt_arrives_whole_through_the_hooks_own_slices(self, prompt) -> None:
        """The payload round-trips: what the hook binds to $PROMPT is what the test put in.

        RED today for the multi-line and field-shaped cases: `sed -n '2p'` takes line one.
        """
        fields = _fields(
            {"session_id": "sid-frame", "prompt": prompt, "effort": {"level": EFFORT_LEVEL}}
        )
        assert fields[PAYLOAD_FIELD] == prompt

    @pytest.mark.parametrize("prompt", ROUND_TRIP_PROMPTS)
    def test_the_scalars_around_it_hold_what_the_envelope_carried(self, prompt) -> None:
        """The scalars are not displaced by the payload's newlines.

        The envelope carries no agent_id, so AGENT_ID must be empty: a prompt line landing
        there is what makes the hook treat a top-level turn as a sub-agent and skip the
        auto-route, the recall briefing and the session publish.
        """
        sid = "sid-frame"
        fields = _fields(
            {"session_id": sid, "prompt": prompt, "effort": {"level": EFFORT_LEVEL}}
        )
        assert fields["SESSION_ID"] == sid
        assert fields["AGENT_ID"] == ""
        assert fields["EFFORT"] == EFFORT_LEVEL

    def test_a_long_multiline_prompt_keeps_its_frame(self) -> None:
        """The regression case, and the risk any reorder carries.

        GREEN TODAY and it must stay green: over 300 characters the parser flattens the
        prompt through `extract_keywords` before printing, so the record is already five
        lines and every scalar already lands correctly. Nothing about this cycle may change
        that, and nothing about it should be read as coverage of the defect.
        """
        assert len(LONG_MULTILINE_PROMPT) > 300, (
            "this fixture only exercises the flattening path while it is over 300 "
            "characters (writ-prompt-parse.py:75); shortening it silently turns this "
            "regression guard into a second copy of the defect case"
        )
        assert "\n" in LONG_MULTILINE_PROMPT
        sid = "sid-long"
        out = _parse(
            {
                "session_id": sid,
                "prompt": LONG_MULTILINE_PROMPT,
                "effort": {"level": EFFORT_LEVEL},
            }
        )
        assert out.returncode == 0, out.stderr
        assert len(out.stdout.split("\n")) - 1 == len(FIELD_CONTRACT)
        fields = _slice_through_the_hook(out.stdout)
        assert fields["SESSION_ID"] == sid
        assert fields["AGENT_ID"] == ""
        assert fields["MODE_HINT"] == "work"
        assert fields["EFFORT"] == EFFORT_LEVEL
        assert fields[PAYLOAD_FIELD] != ""
        assert "\n" not in fields[PAYLOAD_FIELD]


class TestAScalarCannotMoveAFieldBoundary:
    """Capability 6. "It cannot contain a newline" is the assumption that produced this
    defect, so the invariant is enforced at the producer rather than assumed."""

    def test_newline_bearing_scalars_stay_inside_their_own_field(self) -> None:
        _assert_scalars_are_contained(PARSE_PY)

    def test_a_non_string_scalar_does_not_route_to_the_exception_arm(self) -> None:
        """A non-string `agent_id` must still produce a record.

        Sanitizing with `str(v)` is what keeps this true; a bare `.translate` on an int
        would raise inside the try block and send the whole parse to the five-empty-fields
        arm, turning a harmless envelope quirk into a session with no id.
        """
        fields = _fields({"session_id": "sid-1", "agent_id": 123, "prompt": "hi"})
        assert fields["AGENT_ID"] == "123"
        assert fields[PAYLOAD_FIELD] == "hi"


class TestTheMalformedEnvelopeArm:
    """Capability 7. Five empty fields are order-invariant, so this arm needs no edit; the
    test exists because reading the payload as a TAIL makes the field COUNT a hard contract
    and this is where an off-by-one in that count would surface first."""

    def test_malformed_stdin_exits_zero_with_one_empty_field_per_contract(self) -> None:
        out = subprocess.run(
            [sys.executable, str(PARSE_PY)],
            input="{not valid json",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0
        assert out.stdout == "\n" * len(FIELD_CONTRACT)

    def test_every_field_read_through_the_contract_is_empty(self) -> None:
        out = subprocess.run(
            [sys.executable, str(PARSE_PY)],
            input="{not valid json",
            capture_output=True,
            text=True,
            timeout=30,
        )
        fields = _slice_through_the_hook(out.stdout)
        assert fields == {name: "" for name in FIELD_CONTRACT}


# --------------------------------------------------------------------------- #
# Capability 1, 4: the real hook, end to end, judged by its own breadcrumb
# --------------------------------------------------------------------------- #
class TestTheHookReceivesTheWholePrompt:

    def test_a_short_multiline_prompt_arrives_whole(self, tmp_path) -> None:
        """RED today: the breadcrumb reports the length of line one.

        Left side is the hook's own `${#PROMPT}` expansion; right side is `len()` of this
        module's own input literal. Neither is computed from the parser's output.
        """
        result = _run_hook(tmp_path, SHORT_MULTILINE_PROMPT, "frame-whole-multi")
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["breadcrumb"]["prompt_len"] == len(SHORT_MULTILINE_PROMPT)

    def test_a_single_line_prompt_still_arrives_whole(self, tmp_path) -> None:
        """The control: green today, and it must stay green."""
        result = _run_hook(tmp_path, SINGLE_LINE_PROMPT, "frame-whole-single")
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["breadcrumb"]["prompt_len"] == len(SINGLE_LINE_PROMPT)

    def test_prompt_text_cannot_impersonate_the_session_id(self, tmp_path) -> None:
        """Green today and carried as the guard on the reorder: field one is the session id
        no matter what the prompt's lines are shaped like."""
        sid = "frame-real-sid"
        result = _run_hook(tmp_path, IMPERSONATING_PROMPT, sid)
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["breadcrumb"]["session_id"] == sid


class TestTheAutoRouteSurvivesAMultilinePrompt:
    """Capability 2 and 3, and the load-bearing loss this cycle repairs: the correct hint is
    computed from the RAW prompt inside the parser and then discarded by the shell, so a
    short multi-line build request has never auto-routed to work.

    The left side is the mode read back out of the session cache the real helper wrote after
    the real hook ran. The right side is a literal chosen here. Neither is computed from the
    parser's output.
    """

    def test_a_multiline_build_request_routes_to_work(self, tmp_path) -> None:
        """RED today: the shifted AGENT_ID is non-empty, so the hook skips the entire
        auto-route block and the session is left with no mode at all."""
        result = _run_hook(tmp_path, SHORT_MULTILINE_PROMPT, "frame-route-multi")
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["mode"] == "work"
        assert result["cache"].get("mode_source") == "auto"

    def test_a_single_line_build_request_still_routes_to_work(self, tmp_path) -> None:
        result = _run_hook(
            tmp_path,
            "implement the export endpoint from the approved plan",
            "frame-route-build",
        )
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["mode"] == "work"

    def test_a_single_line_audit_request_still_routes_to_investigate(self, tmp_path) -> None:
        result = _run_hook(
            tmp_path, "audit the codebase for security issues", "frame-route-audit"
        )
        assert result["run"].returncode == 0, result["run"].stderr
        assert result["mode"] == "investigate"


# --------------------------------------------------------------------------- #
# Capability 8, 9, 10: the frame, the tail read, and the spawn budget
# --------------------------------------------------------------------------- #
class TestTheParserAndTheHookAgreeOnTheFrame:
    """Three sides, no side computed from another: the order derived from the real parser,
    the order derived from the real hook, and the contract declared at the top of this file.

    Half of this is red today for a reason worth naming so nobody reads the green half as
    proof: the parser and the hook currently AGREE with each other (both put the payload
    second), and what they disagree with is the contract.
    """

    def test_the_parser_prints_the_contract_order(self) -> None:
        _assert_parser_order_matches_contract(PARSE_PY)

    def test_the_hook_reads_the_contract_order(self) -> None:
        slices = _inventory().rag_inject_field_slices()
        assert list(slices) == list(FIELD_CONTRACT), (
            f"the hook assigns {list(slices)}, the contract declares "
            f"{list(FIELD_CONTRACT)}"
        )

    def test_the_hooks_field_names_have_not_decayed(self) -> None:
        """A renamed variable or a deleted assignment must fail here rather than silently
        shrink the population every other assertion in this file is built on."""
        slices = _inventory().rag_inject_field_slices()
        assert set(slices) == set(FIELD_CONTRACT), (
            f"the hook's field set is {sorted(slices)}, the contract's is "
            f"{sorted(FIELD_CONTRACT)}"
        )

    def test_the_two_derivations_agree_with_each_other(self) -> None:
        """Green today (both put the payload second) and after; it is the contract they
        must both move to, and neither may move without the other."""
        inventory = _inventory()
        hook_order = list(inventory.rag_inject_field_slices())
        parser_order = inventory.prompt_parse_field_order()
        assert [FIELD_CONTRACT[name] for name in hook_order] == parser_order, (
            f"the hook reads {hook_order} where the parser prints {parser_order}"
        )


class TestTheHookReadsThePayloadAsATail:

    def test_the_payload_selector_is_the_tail_form(self) -> None:
        _assert_prompt_selector_is_a_tail(HOOK)

    @pytest.mark.parametrize("name", _slice_names())
    def test_no_scalar_selector_uses_a_tail_form(self, name) -> None:
        """Exactly one field may swallow the remainder, and it is the payload. A scalar read
        as a tail would absorb every field after it."""
        assert name is not _MISSING, name
        if name == PAYLOAD_FIELD:
            pytest.skip("the payload is the field that is read as the tail")
        body = _inventory().rag_inject_field_slices()[name]
        assert ",$p" not in body, (
            f"{name} is read as a tail ({body!r}); only {PAYLOAD_FIELD} may be"
        )


class TestTheFieldExtractionSpendsNoInterpreter:
    """Capability 10, as a membership predicate rather than a count, because the plan's cost
    argument is that the reorder adds zero processes and a count would pass a swap of one
    spawn for another."""

    def test_the_whole_block_stays_inside_the_budget(self) -> None:
        _assert_budget(HOOK)

    @pytest.mark.parametrize("name", _slice_names())
    def test_no_single_field_spends_an_interpreter(self, name) -> None:
        assert name is not _MISSING, name
        body = _inventory().rag_inject_field_slices()[name]
        spent = _commands(body)
        assert spent <= ALLOWED_COMMANDS, (
            f"{name} spends {sorted(spent - ALLOWED_COMMANDS)} outside the declared budget "
            f"{sorted(ALLOWED_COMMANDS)}"
        )


# --------------------------------------------------------------------------- #
# Capability 11: every detector above is conditional, proven by mutation
# --------------------------------------------------------------------------- #
class TestEveryDetectorIsConditional:
    """MUTATION AGAINST COPIES UNDER tmp_path, never against the real tree.

    This repo has shipped assertions that could not fail, and both had the same tell: the
    right-hand side was computed from the left-hand side. A detector that cannot be made to
    go red is not coverage, so each one here is handed a deliberately broken artifact and
    required to reject it.
    """

    def test_a_swapped_parser_record_is_seen_by_the_derivation(self, tmp_path) -> None:
        """The derivation reads the file, rather than reporting a remembered order.

        A hardcoded population is blind, not just stale: if this derivation returned a
        literal list, the mutant and the original would agree and every parity assertion in
        this module would be decoration.
        """
        inventory = _inventory()
        copy = _copy(PARSE_PY, tmp_path)
        source = copy.read_text()
        placeholders = re.findall(r"\{(\w+)\}", source)
        assert len(placeholders) >= 2, "no record f-string to mutate"
        first, second = placeholders[0], placeholders[1]
        mutant = source.replace(
            "{" + first + "}\\n{" + second + "}", "{" + second + "}\\n{" + first + "}", 1
        )
        assert mutant != source, "the swap mutation did not apply"
        copy.write_text(mutant)
        assert inventory.prompt_parse_field_order(path=copy) != inventory.prompt_parse_field_order()

    def test_the_parity_detector_rejects_a_swapped_record(self, tmp_path) -> None:
        """Polarity, proven on a synthetic pair the test authored itself: contract order
        passes, a swap of any two fields fails."""
        names = list(FIELD_CONTRACT.values())
        good = tmp_path / "good-parser.py"
        good.write_text("print(f'" + "\\n".join("{" + n + "}" for n in names) + "')\n")
        _assert_parser_order_matches_contract(good)

        swapped = names[1::-1] + names[2:]
        bad = tmp_path / "bad-parser.py"
        bad.write_text("print(f'" + "\\n".join("{" + n + "}" for n in swapped) + "')\n")
        with pytest.raises(AssertionError):
            _assert_parser_order_matches_contract(bad)

    def test_narrowing_the_tail_selector_reds_the_tail_detector(self, tmp_path) -> None:
        """RED TODAY, and the failure is the missing production behaviour: there is no tail
        selector in the hook yet, so the mutation has nothing to narrow."""
        copy = _copy(HOOK, tmp_path)
        source = copy.read_text()
        mutant = source.replace(TAIL_SELECTOR, f"{PAYLOAD_POSITION}p")
        if mutant == source:
            pytest.fail(
                f"skeleton: the hook does not read {PAYLOAD_FIELD} as a tail yet (no "
                f"{TAIL_SELECTOR!r} to narrow), so this detector cannot be proven "
                "conditional. plan.md ## Files: the hook reslices to sed -n "
                f"'{TAIL_SELECTOR}'.",
                pytrace=False,
            )
        copy.write_text(mutant)
        with pytest.raises(AssertionError):
            _assert_prompt_selector_is_a_tail(copy)

    def test_removing_the_scalar_sanitizer_reds_the_containment_detector(
        self, tmp_path
    ) -> None:
        """RED TODAY for the same reason: the sanitizer does not exist, so there is nothing
        to remove."""
        copy = _copy(PARSE_PY, tmp_path)
        source = copy.read_text()
        mutant = re.sub(r"_one_line\((?P<inner>[^()]*)\)", r"\g<inner>", source)
        if mutant == source:
            pytest.fail(
                "skeleton: the parser has no _one_line(...) call to remove, so the "
                "delimiter-bearing scalar detector cannot be proven conditional. plan.md "
                "## Files: the four scalars are made newline-free inside the parser.",
                pytrace=False,
            )
        copy.write_text(mutant)
        with pytest.raises(AssertionError):
            _assert_scalars_are_contained(copy)

    def test_a_jq_pipe_reds_the_budget_detector(self, tmp_path) -> None:
        """The budget guard that stops a later 'just pipe it through jq' fix."""
        inventory = _inventory()
        copy = _copy(HOOK, tmp_path)
        source = copy.read_text()
        body = inventory.rag_inject_field_slices(path=copy)[PAYLOAD_FIELD]
        mutant = source.replace(f"$({body})", f"$({body} | jq -r .)", 1)
        assert mutant != source, "the jq mutation did not apply"
        copy.write_text(mutant)
        with pytest.raises(AssertionError):
            _assert_budget(copy)


class TestTheDerivationsThemselvesAreSound:
    """The two new inventory derivations are loud rather than silently empty, and they read
    the artifact they claim to read."""

    def test_the_parser_derivation_is_not_empty(self) -> None:
        assert len(_inventory().prompt_parse_field_order()) == len(FIELD_CONTRACT)

    def test_the_hook_derivation_is_not_empty(self) -> None:
        assert len(_inventory().rag_inject_field_slices()) == len(FIELD_CONTRACT)

    def test_a_parser_with_no_record_raises_rather_than_returning_nothing(
        self, tmp_path
    ) -> None:
        empty = tmp_path / "no-record.py"
        empty.write_text("print('no fields here')\n")
        with pytest.raises(ValueError):
            _inventory().prompt_parse_field_order(path=empty)

    def test_a_record_joined_by_something_other_than_a_newline_raises(
        self, tmp_path
    ) -> None:
        """The consumer slices by line, so a record joined by anything else is a frame this
        derivation must refuse to describe rather than silently mis-report."""
        names = list(FIELD_CONTRACT.values())
        odd = tmp_path / "odd-separator.py"
        odd.write_text("print(f'" + "|".join("{" + n + "}" for n in names) + "')\n")
        with pytest.raises(ValueError):
            _inventory().prompt_parse_field_order(path=odd)

    def test_the_hook_derivation_reads_the_file_it_is_given(self, tmp_path) -> None:
        copy = _copy(HOOK, tmp_path)
        source = copy.read_text()
        slices = _inventory().rag_inject_field_slices(path=copy)
        renamed = source.replace(f"{PAYLOAD_FIELD}=$({slices[PAYLOAD_FIELD]})",
                                 f"RENAMED=$({slices[PAYLOAD_FIELD]})", 1)
        assert renamed != source, "the rename mutation did not apply"
        copy.write_text(renamed)
        assert PAYLOAD_FIELD not in _inventory().rag_inject_field_slices(path=copy)
