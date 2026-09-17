"""The `effort` hop is dead end to end, and the proof is the artifact each consumer reads.

THE MEASUREMENT. `bin/lib/writ-prompt-parse.py` is reached by exactly one Claude Code
event, `UserPromptSubmit`. Across the 14,101 captured envelopes in
`~/.claude/writ-blackbox.jsonl` there are 444 `UserPromptSubmit` envelopes and ZERO of them
carry an `effort` key, while `PreToolUse` (3,638), `PostToolUse` (2,367), `SubagentStop`
(564) and `Stop` (81) carry it in bulk. So the parser's `eff = data.get('effort')` has never
seen a key, the hook's `EFFORT` has always been the empty string, the request field has
always been `""`, and the telemetry branch has always omitted the key. The whole chain is
dead, not just the hop nearest the `F841` at `writ/server/routes/query.py:226`.

THE TRAP, named because it cost the first pass and it will cost the next reader: the
blackbox stores each envelope as an ESCAPED JSON STRING (`payload: sys.stdin.read()` in
`bin/lib/common.sh`), so the keys appear as `\\"effort\\"` and a naive grep for `"effort"`
returns zero on a file holding thousands. The counts above come from parsing the payload,
never from grepping it.

A CHAIN TEST, NOT A PRODUCER TEST. This repository burned three cycles on a green producer
test while one module downstream discarded the value, so nothing here asserts on the
producer alone. The request assertions read the ACTUAL JSON body the hook builds and the
telemetry assertions read the ACTUAL rows the hook emits, both produced by running the
hook's OWN builder blocks, lifted verbatim out of `hooks/scripts/writ-rag-inject.sh` and
executed in a real bash. Neither arm is reimplemented here, because a guard that hand-rolls
one of the two paths it compares proves only that the code matches a model of the route.

WHICH ARM RAN IS PROVEN POSITIVELY. Each arm is selected by restricting `PATH` to a
directory holding a symlink to that arm's interpreter alone, so a body produced under the
jq-only PATH cannot have come from python and vice versa. That is stronger than trusting
`WRIT_NO_JQ`, which only asks the hook nicely.

THE STALE-EFFORT FIXTURE. Every builder run here sets `EFFORT` to a non-empty value in the
surrounding shell. Today both builders forward it and every assertion below that names
`effort` is red. After the deletion the variable is simply ignored, which is the only way to
tell "the key is gone" apart from "the key was empty on this input".

NO COUNT LITERAL. The record frame goes from five fields to four, and every dependent value
is `len(FIELD_CONTRACT)` from `tests/test_prompt_parse_field_frame.py`, the single
hand-authored statement of the frame. Removing its `EFFORT` row is the one edit; the tail
selector, the malformed arm's output and the parser and hook parity assertions already
derive from it and are asserted in that module, not copied here.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_prompt_parse_field_frame import (
    FIELD_CONTRACT,
    PAYLOAD_FIELD,
    TAIL_SELECTOR,
)

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
PARSE_PY = REPO / "bin" / "lib" / "writ-prompt-parse.py"
QUERY_PY = REPO / "writ" / "server" / "routes" / "query.py"

BASH = shutil.which("bash")
JQ = shutil.which("jq")
PYTHON3 = shutil.which("python3") or sys.executable

# A non-empty effort value planted in the surrounding shell and in the envelope. Its job is
# to be VISIBLE if any hop still forwards it, so "" would prove nothing.
STALE_EFFORT = "xhigh"

# The two builder regions, addressed by the real text that opens and closes them rather
# than by line number. Each marker is asserted to occur exactly once, so a moved or
# reworded boundary fails by name instead of silently slicing the wrong bytes.
BUNDLE_REQUEST_REGION = ('\nBUNDLE_REQUEST=""\n', "\n# writ_http_post:")
FRICTION_ROWS_REGION = ('\nFRICTION_ROWS=""\n', '\nif [ -n "$FRICTION_ROWS" ]; then')

# The shape a live /prompt-bundle really returns (the first entry of
# tests/test_friction_rows_jq.py::BUNDLES, captured 2026-08-07). One bundle, because the
# shape matrix is that module's job and this module's job is the effort key.
LIVE_BUNDLE = json.dumps(
    {
        "broad_meta": {"rule_ids": ["A", "B"], "cost": 600},
        "ao_meta": {"tokens": 1220, "count": 12, "rule_ids": ["X"] * 12},
        "method_meta": {"rule_ids": ["M1"], "cost": 320, "query_source": "methodology"},
    }
)

# The keys /prompt-bundle's request model still declares once `effort` is gone. Written
# here as the anti-vacuity companion to the absence assertion: "no effort key" is also
# satisfied by a body that is empty or malformed, and this is what rules that out.
EXPECTED_REQUEST_KEYS = {
    "session_id",
    "mode",
    "prompt",
    "project_root",
    "always_on_filter",
    "include_ranked",
}

_MISSING = "<tests/_inventory.py::rag_inject_python_blocks() is unavailable>"

ARMS = [
    pytest.param(
        "jq",
        marks=pytest.mark.skipif(JQ is None, reason="jq not installed"),
        id="jq",
    ),
    pytest.param("python", id="python-fallback"),
]


# --------------------------------------------------------------------------- #
# helpers: the hook's own builder blocks, run in a real bash
# --------------------------------------------------------------------------- #
def _inventory():
    """`tests/_inventory.py` with this cycle's derivation, or a loud failure."""
    import tests._inventory as inventory

    if not hasattr(inventory, "rag_inject_python_blocks"):
        pytest.fail(
            "skeleton: tests/_inventory.py has no rag_inject_python_blocks yet (plan.md "
            "## Files assigns it there: the hook's inline `python3 -c` blocks as a MAP "
            "keyed by the shell variable each one fills, so a renamed variable or a "
            "deleted block fails by name rather than shrinking the population silently). "
            "It must return the exact string bash hands to `python3 -c`, including the "
            "leading newline, or the byte-equality assertion in this module compares "
            "something the hook does not run.",
            pytrace=False,
        )
    return inventory


def _region(bounds: tuple[str, str], *, hook: Path = HOOK) -> str:
    """The real hook text between two markers, each required to be unique."""
    source = hook.read_text(encoding="utf-8")
    start, end = bounds
    for marker in bounds:
        found = source.count(marker)
        assert found == 1, (
            f"{hook.name} holds the region marker {marker!r} {found} time(s), not once; "
            "this module executes the hook's OWN builder text and a marker that moved or "
            "was reworded must fail by name rather than slice the wrong bytes"
        )
    opening = source.index(start) + len(start)
    closing = source.index(end)
    assert closing > opening, (
        f"the region markers in {hook.name} are out of order: {start!r} must precede "
        f"{end!r}"
    )
    body = source[opening:closing]
    assert body.strip(), f"the region between {start!r} and {end!r} in {hook.name} is empty"
    return body


def _arm_path(tmp_path: Path, arm: str) -> str:
    """A PATH holding one interpreter, so the arm that produced the output is a fact.

    bash is invoked by its absolute path, and `printf`, `echo` and `command` are builtins,
    so nothing else needs to be reachable. A body produced under the jq-only PATH cannot
    have come from the python fallback.
    """
    binary = {"jq": JQ, "python": PYTHON3}[arm]
    assert binary, f"no {arm} interpreter on this machine"
    directory = tmp_path / f"{arm}-only-bin"
    directory.mkdir(exist_ok=True)
    link = directory / Path(binary).name
    if not link.exists():
        link.symlink_to(binary)
    return str(directory)


def _run_region(body: str, prelude: str, env: dict[str, str], path: str) -> str:
    """Run a lifted region under the hook's own shell options and return its output."""
    assert BASH, "no bash on this machine"
    script = "set -euo pipefail\n" + prelude + body + "\n"
    child = dict(os.environ)
    child.update(env)
    child["PATH"] = path
    run = subprocess.run(
        [BASH, "-s"],
        input=script,
        capture_output=True,
        text=True,
        env=child,
        timeout=60,
    )
    assert run.returncode == 0, (
        f"the hook's own builder block failed under PATH={path}: {run.stderr[:800]}"
    )
    return run.stdout


def _build_request(tmp_path: Path, arm: str) -> dict:
    """The ACTUAL request body the real hook builds, parsed.

    The prelude sets only the variables the region reads, from the environment so no value
    passes through shell quoting. EFFORT is set to a non-empty value deliberately.
    """
    prelude = (
        'SESSION_ID="$WRIT_T_SID"\n'
        'CURRENT_MODE="$WRIT_T_MODE"\n'
        'PROMPT="$WRIT_T_PROMPT"\n'
        'EFFORT="$WRIT_T_EFFORT"\n'
        '_PROJECT_ROOT="$WRIT_T_PROOT"\n'
        "_AO_FILTER_BOOL=true\n"
        "_INCLUDE_RANKED_BOOL=true\n"
        'BUNDLE_REQUEST=""\n'
    )
    env = {
        "WRIT_T_SID": "sid-effort",
        "WRIT_T_MODE": "work",
        "WRIT_T_PROMPT": "implement the export endpoint",
        "WRIT_T_EFFORT": STALE_EFFORT,
        "WRIT_T_PROOT": str(tmp_path / "project"),
    }
    if arm == "python":
        env["WRIT_NO_JQ"] = "1"
    else:
        env.pop("WRIT_NO_JQ", None)
    tail = '\nprintf \'%s\' "$BUNDLE_REQUEST"\n'
    out = _run_region(
        _region(BUNDLE_REQUEST_REGION) + tail, prelude, env, _arm_path(tmp_path, arm)
    )
    assert out.strip(), (
        f"the {arm} arm of the hook's /prompt-bundle request builder produced nothing; "
        "every assertion about the body would pass vacuously"
    )
    return json.loads(out)


def _build_rows(tmp_path: Path, arm: str, bundle: str = LIVE_BUNDLE) -> list[dict]:
    """The ACTUAL friction rows the real hook emits, parsed."""
    prelude = (
        'WRIT_DIR="$WRIT_T_DIR"\n'
        'WRIT_HOOK_LOG_SINK="$WRIT_T_SINK"\n'
        'SESSION_ID="$WRIT_T_SID"\n'
        'CURRENT_MODE="$WRIT_T_MODE"\n'
        'EFFORT="$WRIT_T_EFFORT"\n'
        'BUNDLE="$WRIT_T_BUNDLE"\n'
        'FRICTION_ROWS=""\n'
        '_FRICTION_ROWS_OK=""\n'
    )
    env = {
        "WRIT_T_DIR": str(REPO),
        "WRIT_T_SINK": str(tmp_path / "hook.log"),
        "WRIT_T_SID": "sid-effort",
        "WRIT_T_MODE": "work",
        "WRIT_T_EFFORT": STALE_EFFORT,
        "WRIT_T_BUNDLE": bundle,
    }
    if arm == "python":
        env["WRIT_NO_JQ"] = "1"
    else:
        env.pop("WRIT_NO_JQ", None)
    tail = '\nprintf \'%s\' "$FRICTION_ROWS"\n'
    out = _run_region(
        _region(FRICTION_ROWS_REGION) + tail, prelude, env, _arm_path(tmp_path, arm)
    )
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _parse(envelope: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PARSE_PY)],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _prompt_bundle_function() -> ast.AsyncFunctionDef:
    tree = ast.parse(QUERY_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "prompt_bundle":
            return node
    raise AssertionError(f"no `async def prompt_bundle` in {QUERY_PY}")


def _request_attributes_read_by_the_route() -> set[str]:
    return {
        node.attr
        for node in ast.walk(_prompt_bundle_function())
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    }


def _inline_block_names() -> list:
    """Collection-time population for the per-block cases.

    Never raises: `_inventory()` fails the calling TEST, and a failure raised here would
    abort COLLECTION of the whole module, which reads as a broken test file rather than as
    the missing production behaviour it is.
    """
    try:
        import tests._inventory as inventory

        blocks = sorted(inventory.rag_inject_python_blocks())
    except BaseException:  # noqa: BLE001
        return [pytest.param(_MISSING, id="inline-blocks-unavailable")]
    return blocks or [pytest.param(_MISSING, id="inline-blocks-empty")]


# --------------------------------------------------------------------------- #
# the frame: the contract loses a field, and every derived value follows
# --------------------------------------------------------------------------- #
class TestTheRecordFrameDeclaresNoEffortField:
    """The anchor edit. `FIELD_CONTRACT` is the ONE hand-authored statement of the record
    and it lives in another module on purpose, so the parity assertions that judge the
    parser and the hook cannot be moved to agree with a broken derivation in one diff.

    Removing its `EFFORT` row is the whole edit here. Nothing in this class restates the
    field count, the tail selector or the malformed arm's output, because
    tests/test_prompt_parse_field_frame.py already derives all three from
    `len(FIELD_CONTRACT)` and a second copy would be free to drift from the first.
    """

    def test_the_contract_declares_no_effort_field(self) -> None:
        assert "EFFORT" not in FIELD_CONTRACT, (
            "FIELD_CONTRACT still declares an EFFORT field. UserPromptSubmit is the only "
            "event that reaches the parser and none of the 444 captured UserPromptSubmit "
            "envelopes carries an `effort` key, so this field has never held a value. It "
            "is the anchor: while it stands, the parser and the hook agree with it and "
            "the dead hop stays"
        )

    def test_the_payload_is_last_so_the_tail_selector_follows_the_contract(self) -> None:
        """The reason no literal has to move when the frame shrinks.

        `TAIL_SELECTOR` is derived from the payload's POSITION; this states the property
        that makes that position equal to the field count, which is what carries the
        selector from `5,$p` to `4,$p` with nothing hand-edited.
        """
        assert list(FIELD_CONTRACT)[-1] == PAYLOAD_FIELD, (
            f"{PAYLOAD_FIELD} must be the last field: it is the only one that may contain "
            f"the record's delimiter. Contract order: {list(FIELD_CONTRACT)}"
        )
        assert TAIL_SELECTOR == f"{len(FIELD_CONTRACT)},$p"

    def test_the_hook_reads_the_payload_with_the_contracts_own_selector(self) -> None:
        """The consumer half, asserted against the derived selector rather than a literal."""
        import tests._inventory as inventory

        slices = inventory.rag_inject_field_slices()
        assert PAYLOAD_FIELD in slices, (
            f"the hook has no {PAYLOAD_FIELD} assignment over $PARSED: {sorted(slices)}"
        )
        assert TAIL_SELECTOR in slices[PAYLOAD_FIELD], (
            f"the hook reads {PAYLOAD_FIELD} with {slices[PAYLOAD_FIELD]!r}, but the "
            f"contract's {len(FIELD_CONTRACT)} fields put the tail at {TAIL_SELECTOR!r}"
        )

    def test_the_hook_binds_no_shell_variable_for_effort(self) -> None:
        import tests._inventory as inventory

        slices = inventory.rag_inject_field_slices()
        assert slices, "no field assignments derived from the hook"
        assert "EFFORT" not in slices, (
            f"the hook still slices an EFFORT field out of the parser record: "
            f"{slices.get('EFFORT')!r}"
        )


class TestTheParserKeepsTheEffortLevelOutOfTheRecord:
    """Behavioural, on the real parser and a real envelope, because "the source no longer
    mentions effort" is a statement about text and this is a statement about the record."""

    def test_no_printed_field_holds_the_envelopes_effort_level(self) -> None:
        out = _parse(
            {
                "session_id": "sid-effort",
                "prompt": "hello there world",
                "effort": {"level": STALE_EFFORT},
            }
        )
        assert out.returncode == 0, out.stderr
        fields = out.stdout.split("\n")
        assert STALE_EFFORT not in fields, (
            f"the parser printed the envelope's effort level as a field: {out.stdout!r}"
        )

    def test_an_envelope_carrying_effort_still_parses_normally(self) -> None:
        """Anti-vacuity: the absence above must not be an absence of output.

        An envelope shaped exactly like a real Claude Code tool event (effort present)
        must still yield the session id in field one and the prompt as the tail.
        """
        out = _parse(
            {
                "session_id": "sid-effort",
                "prompt": "hello there world",
                "effort": {"level": STALE_EFFORT},
            }
        )
        assert out.returncode == 0, out.stderr
        lines = out.stdout.split("\n")
        assert lines[0] == "sid-effort"
        assert lines[len(FIELD_CONTRACT) - 1] == "hello there world"


# --------------------------------------------------------------------------- #
# the request body: the artifact the daemon actually receives
# --------------------------------------------------------------------------- #
class TestTheRequestBodyTheHookReallyBuilds:
    """Both arms are the hook's own text, executed. The arm is selected by a PATH holding
    one interpreter, so which builder produced a given body is a fact rather than a hope."""

    @pytest.mark.parametrize("arm", ARMS)
    def test_the_built_body_carries_no_effort_key(self, tmp_path, arm) -> None:
        body = _build_request(tmp_path, arm)
        assert "effort" not in body, (
            f"the {arm} arm of the hook still sends `effort` to /prompt-bundle: {body!r}. "
            "EFFORT was deliberately non-empty in the surrounding shell, so this is the "
            "forwarding hop and not an empty value passing through"
        )

    @pytest.mark.parametrize("arm", ARMS)
    def test_the_built_body_still_carries_every_field_the_route_reads(
        self, tmp_path, arm
    ) -> None:
        """Anti-vacuity for the assertion above: an empty or truncated body would also
        carry no `effort` key.

        A SUBSET, deliberately, so this stays GREEN today and after. An exact set equality
        here would be red today for the very reason the absence test is red, and then the
        absence test would have no independent witness that the builder ran at all.
        """
        body = _build_request(tmp_path, arm)
        missing = EXPECTED_REQUEST_KEYS - set(body)
        assert not missing, (
            f"the {arm} arm built {sorted(body)} and is missing {sorted(missing)}"
        )
        assert body["session_id"] == "sid-effort"
        assert body["prompt"] == "implement the export endpoint"
        assert body["always_on_filter"] is True
        assert body["include_ranked"] is True

    @pytest.mark.skipif(JQ is None, reason="jq not installed")
    def test_both_arms_build_the_same_body(self, tmp_path) -> None:
        """The seam's contract: absence of jq changes speed, never the body."""
        assert _build_request(tmp_path, "jq") == _build_request(tmp_path, "python")


# --------------------------------------------------------------------------- #
# the friction rows: the artifact the 365-day audit stream actually receives
# --------------------------------------------------------------------------- #
class TestTheFrictionRowsTheHookReallyEmits:

    @pytest.mark.parametrize("arm", ARMS)
    def test_no_emitted_row_carries_an_effort_key(self, tmp_path, arm) -> None:
        rows = _build_rows(tmp_path, arm)
        carrying = [row for row in rows if "effort" in row]
        assert carrying == [], (
            f"the {arm} row builder still writes `effort` into the audit stream: "
            f"{carrying!r}. EFFORT was deliberately non-empty in the surrounding shell"
        )

    @pytest.mark.parametrize("arm", ARMS)
    def test_the_live_bundle_shape_still_emits_a_row_per_channel(
        self, tmp_path, arm
    ) -> None:
        """Anti-vacuity: a builder that emitted nothing would satisfy the absence above."""
        rows = _build_rows(tmp_path, arm)
        assert [row["event"] for row in rows] == [
            "rag_query",
            "always_on_inject",
            "rag_query",
        ], rows

    @pytest.mark.skipif(JQ is None, reason="jq not installed")
    def test_both_arms_emit_the_same_parsed_rows(self, tmp_path) -> None:
        assert _build_rows(tmp_path, "jq") == _build_rows(tmp_path, "python")

    @pytest.mark.skipif(JQ is None, reason="jq not installed")
    def test_the_jq_filter_takes_no_effort_argument(self, tmp_path) -> None:
        """The filter's invocation contract, read off the invocation the hook really makes.

        `--arg effort` with no `$effort` in the filter is a jq error, and the reverse is a
        null reference, so the two halves must move together.
        """
        invocation = _region(FRICTION_ROWS_REGION)
        assert "--arg effort" not in invocation, (
            "the hook still passes --arg effort to friction-rows.jq"
        )
        filter_text = (REPO / "bin" / "lib" / "friction-rows.jq").read_text(
            encoding="utf-8"
        )
        assert "$effort" not in filter_text, (
            "friction-rows.jq still declares an $effort parameter the hook no longer sends"
        )


class TestTheCopiedPythonBuilderIsTheBlockTheHookRuns:
    """The pre-existing hazard this cycle is obliged to close, because it edits both halves.

    `tests/test_friction_rows_jq.py::PY_BUILDER` is a HAND-MAINTAINED copy of the hook's
    python fallback, and that module's parity oracle compares the jq filter against the
    copy. If the copy drifts, the oracle validates a fiction, which is this repository's
    "a test that models a path is blind to it" failure in its purest form.

    The two sides are independent: the left is the hook file, the right is the test
    module's own literal, and neither is computed from the other. Verified byte-identical
    on 2026-09-16 before this assertion was written, so it starts from a true baseline and
    a later failure means a real drift.
    """

    def test_the_derivation_names_both_blocks_this_cycle_edits(self) -> None:
        blocks = _inventory().rag_inject_python_blocks()
        for name in ("BUNDLE_REQUEST", "FRICTION_ROWS"):
            assert name in blocks, (
                f"no inline python block filling {name} was derived from the hook: "
                f"{sorted(blocks)}. A renamed shell variable or a deleted block must fail "
                "BY NAME here rather than shrink the population every parity assertion "
                "rests on"
            )

    @pytest.mark.parametrize("name", _inline_block_names())
    def test_every_derived_block_is_non_empty(self, name) -> None:
        assert name is not _MISSING, name
        assert _inventory().rag_inject_python_blocks()[name].strip(), (
            f"the block filling {name} derived as empty"
        )

    def test_the_copy_in_the_parity_module_is_byte_equal_to_the_hooks_block(self) -> None:
        import tests.test_friction_rows_jq as parity

        block = _inventory().rag_inject_python_blocks()["FRICTION_ROWS"]
        assert parity.PY_BUILDER == block, (
            "tests/test_friction_rows_jq.py::PY_BUILDER has drifted from the block the "
            "hook really runs, so that module's jq/python parity oracle is comparing the "
            "filter against a fiction. The derivation returns the exact string bash hands "
            "to `python3 -c`, leading newline included"
        )


# --------------------------------------------------------------------------- #
# the daemon end of the chain
# --------------------------------------------------------------------------- #
class TestTheModelAndTheRouteCarryNoEffort:

    def test_the_request_model_declares_no_effort_field(self) -> None:
        from writ.server.models import PromptBundleRequest

        assert "effort" not in PromptBundleRequest.model_fields, (
            "PromptBundleRequest still declares `effort`, the field the hook stopped "
            "sending and the route never read"
        )

    def test_the_model_still_declares_the_fields_the_route_does_read(self) -> None:
        """Anti-vacuity: a model stripped of everything would satisfy the absence above."""
        from writ.server.models import PromptBundleRequest

        assert EXPECTED_REQUEST_KEYS <= set(PromptBundleRequest.model_fields)

    def test_the_route_reads_no_effort_attribute_off_the_request(self) -> None:
        attributes = _request_attributes_read_by_the_route()
        assert attributes, (
            "no `request.<attr>` reads found in prompt_bundle; this scan would then pass "
            "on any route at all"
        )
        assert "effort" not in attributes, (
            "writ/server/routes/query.py::prompt_bundle still reads request.effort (the "
            f"F841 at line 226); attributes read: {sorted(attributes)}"
        )

    def test_the_route_still_reads_the_attributes_it_needs(self) -> None:
        attributes = _request_attributes_read_by_the_route()
        assert {"session_id", "project_root", "include_ranked"} <= attributes


class TestAHookThatStillSendsEffortIsStillAccepted:
    """`PromptBundleRequest` is a plain pydantic BaseModel with NO `extra="forbid"`, so a
    hook that still sends `effort` after the daemon has been restarted gets a 200 and the
    key is ignored. That ordering tolerance is what makes this safe to ship without a
    lockstep restart, and it is pinned rather than assumed: adding `extra="forbid"` later,
    or replacing the model with one that validates strictly, would start 422-ing every
    not-yet-updated machine on its next prompt.

    GREEN BEFORE AND AFTER, deliberately. This is the one capability in the module that is
    not a red today; its job is to stop the deletion from taking the tolerance with it.
    """

    @staticmethod
    def _stub(monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import writ.server as server
        import writ.server.routes.query as qroute

        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(
            server.writ_session,
            "_read_cache",
            lambda sid: {
                "loaded_rule_ids_by_phase": {},
                "current_phase": "",
                "loaded_rule_ids": [],
                "remaining_budget": 1500,
                "last_injected_rule_ids": [],
                "detected_domain": "",
            },
        )
        monkeypatch.setattr(
            qroute, "query_rules", AsyncMock(return_value={"rules": [], "mode": "standard"})
        )
        monkeypatch.setattr(
            qroute,
            "always_on_bundle",
            AsyncMock(return_value={"rules": [], "total_tokens": 0}),
        )
        monkeypatch.setattr(server.writ_session, "cmd_update", MagicMock())
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")
        return server

    @pytest.mark.asyncio
    async def test_a_posted_body_carrying_effort_still_returns_the_normal_response(
        self, monkeypatch
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        server = self._stub(monkeypatch)
        body = {
            "session_id": "stale-hook",
            "mode": "",
            "prompt": "implement the export endpoint",
            "effort": STALE_EFFORT,
            "project_root": "",
            "always_on_filter": True,
            "include_ranked": True,
        }
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            response = await client.post("/prompt-bundle", json=body)
        assert response.status_code == 200, (
            "a hook updated after the daemon still posts `effort`; rejecting it would "
            f"break every not-yet-restarted machine. Got {response.status_code}: "
            f"{response.text[:400]}"
        )
        data = response.json()
        for key in (
            "always_on_block",
            "rules_text",
            "methodology_block",
            "nudge",
            "error",
            "broad_meta",
            "ao_meta",
            "method_meta",
        ):
            assert key in data, key
        assert data["error"] is False

    @pytest.mark.asyncio
    async def test_a_body_missing_a_required_field_is_still_rejected(
        self, monkeypatch
    ) -> None:
        """Anti-vacuity for the tolerance: the route must not accept ANYTHING. A missing
        `session_id` is still a 422, so the 200 above is tolerance of an extra key rather
        than validation that stopped running."""
        from httpx import ASGITransport, AsyncClient

        server = self._stub(monkeypatch)
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            response = await client.post("/prompt-bundle", json={"prompt": "x"})
        assert response.status_code == 422, response.text[:400]

    def test_the_model_ignores_an_unknown_key_rather_than_storing_it(self) -> None:
        from writ.server.models import PromptBundleRequest

        built = PromptBundleRequest(session_id="stale-hook", effort=STALE_EFFORT)
        assert "effort" not in built.model_dump(), (
            "the model still carries `effort` in its dump, so the field was not removed"
        )
