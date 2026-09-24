"""tests/test_approval_rejection_reason_recorded.py

Pins the defect in plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3: a rejected advance
spends the human's approval and sends them back to retype it, and the
`approval_pattern_match` friction row it writes today records `outcome: "rejected"`
with no reason at all. Measured on a real project (upgrade.2ndswing.com,
2026-09-21/22): three such rows, and a search of every live and archived log in that
window for any validation-failure record returns exactly one, from a DIFFERENT
project. The reason already exists at the logging site -- `GATE_ERROR`, printed to the
user at hooks/scripts/auto-approve-gate.sh:539 -- and is already passed into the row
builder as `sys.argv[6]` (hooks/scripts/auto-approve-gate.sh:507), where it is used
only as a truthiness test to pick the outcome string and then dropped. `TOKEN_SPENT`
is likewise already in scope (line 438/491) and unrecorded.

THE FIX THIS FILE PINS (## Analysis): the row builder's inline python program gains a
seventh argument, `${TOKEN_SPENT:-}`, and two conditional fields on the entry dict:

  * `reason`: sys.argv[6] with tabs, CR and LF flattened to single spaces, stripped,
    truncated to 300 characters, and set ONLY when the result is non-empty.
  * `token_spent`: JSON `true` when argv[7] == "true", JSON `false` when argv[7] ==
    "false", and the KEY OMITTED (not null) otherwise -- the tri-state
    gate_advance_outcome._token_spent already documents: empty means the server did
    not say.

`advanced_to` still selects `outcome == "advanced-><phase>"`, `rejected` still selects
`"rejected"`, and the fallback is still `"ask-prompt-emitted"` -- BYTE-IDENTICAL to
today, because existing analyzers count these strings. The stdout lines at 533-547 are
UNTOUCHED: the user-facing REJECTED line keeps the full, untruncated reason; only the
persisted row is bounded. The `approval_pattern_miss` row (line 365's MISS_EXTRA dict)
gains one more key, `'tier': 'embedded'`, because every miss is logged inside the
`embedded)` arm only and the row itself does not currently say so.

RED TODAY, by construction: `hooks/scripts/auto-approve-gate.sh`'s row-builder python
program does not read or write `reason` or `token_spent` at all, and MISS_EXTRA does
not carry `tier`. Every test in this file is expected to fail against the code on disk
until the two-file change in plan.md's ## Files lands.

WHAT WOULD MAKE THIS SUITE PASS VACUOUSLY, named once here so no individual test has
to re-earn the point:
  * asserting only that a key EXISTS, never its exact value (a `reason` key present
    but empty, or a `token_spent` key present but always `false`, would satisfy a
    sloppy existence check);
  * asserting the omission of a field (`no reason key`, `token_spent absent`) with no
    sibling case in the SAME class proving the builder can also WRITE that field --
    a builder that never emits either field at all would pass every negative
    assertion in this file and fail nothing;
  * reading a row through `json.loads` on one hand-picked line instead of asserting
    the raw stream file holds exactly one line -- a multi-line reason could forge a
    second JSON line and every per-field assertion above would still "pass" by
    reading only the first;
  * proving the miss-tier population by a hardcoded count of emit sites instead of a
    detector that scans the hook SOURCE, which is blind to a second site added later
    (closed here by TestMissTierDetectorConditionalByMutation's mutation proof);
  * trusting "the mock daemon was never called" as an absence rather than reading a
    POSITIVE count off the stand-in's own counter (see THE ISOLATION TRAP below).

THE ISOLATION TRAP (plan.md ## Test design, and the reason this module exists as a
standalone file rather than reusing another suite's daemon fixture verbatim).
`bin/lib/common.sh:1931-1944` sets `WRIT_CURL_TRANSPORT="--unix-socket ..."` whenever
`$HOME/.cache/writ/run/writ.sock` exists AND `WRIT_SOCKET` is set in the environment --
even when that value was merely INHERITED from the operator's own shell, not chosen by
this test. An explicit `WRIT_HOST`/`WRIT_PORT` override wins only when `WRIT_SOCKET` is
UNSET. So a test that stands up a local HTTP server, names its port via `WRIT_PORT`, and
leaves `WRIT_SOCKET` set has curl silently ignore the URL host and POST to the
operator's REAL daemon -- the same defect class that once wrote 182 records into a
production graph (see the module-level `MEMORY.md` keystone on this exact failure
mode). Every helper in this file that drives the daemon therefore:
  * pops `WRIT_SOCKET` from the subprocess environment entirely (not merely leaves it
    unset in the dict literal -- it must not survive from `os.environ`);
  * sets `WRIT_HOST` and `WRIT_PORT` explicitly to the stand-in's own ephemeral port;
  * counts every POST the stand-in receives, and a dedicated test
    (`TestDaemonIsolationIsAPositiveSignal`) asserts that count is exactly one, so "we
    did not touch the live daemon" is read off a number, not inferred from silence.

ISOLATION CONTRACT for every hook subprocess this file spawns (`pytestmark` below,
plus per-test setup): `WRIT_LOG_ROOT`, `WRIT_LOG_PROJECT`, `WRIT_CACHE_DIR`, `HOME` and
the process cwd are all rooted under `tmp_path`; every session id is uuid-suffixed
(`_sid`); every mint is wrapped in `_mint_cleanup`, with the autouse `_no_leaked_gate_tokens`
sweep (confined to session ids that cannot belong to a live human session, per
tests/_gate_token_leak.py) as the safety net -- `gate_token_path` hardcodes `/tmp`
regardless of `WRIT_CACHE_DIR`, so nothing here relies on cache-dir isolation alone to
keep this suite off a real approval file.

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
Per ## Rules Applied (this cycle's plan.md): CLEAN-LOG-001 (structured queryable
fields, not a smeared string; the flatten-and-bound step keeps one record per line) and
ENF-SYS-005 (durability is the load-bearing claim, so every test here drives the REAL
hook subprocess against a REAL local HTTP stand-in and reads the row back through the
REAL router -- no mocked python function stands in for either).

THE MEASURED LIMIT ON CAPABILITY 6 IS NOW DISCHARGED, by plan.md's follow-up cycle
2412ba38-51e1-4b73-895b-7b240a3c21d3 ("a multi-line refusal must reach the rejection
branch, not the outage branch"). The paragraph this replaces recorded, as a permanent
limit, that a refusal text carrying a raw LINE FEED could never reach `GATE_ERROR` at
all: `gate_advance_outcome.py` prints its classification as one tab-separated line with
the error LAST, and the hook read the verdict with an unguarded `cut -f1` over that
WHOLE multi-line output, so a line feed inside the error made `$OUTCOME` itself
multi-line, the `[ "$OUTCOME" = "rejected" ]` comparison false, and the turn recorded
`ask-prompt-emitted` with no reason at all. That was measured, not theoretical (see this
docstring's opening paragraph). It is FALSE as of 2412ba38's fix: the four fixed
classifier fields (outcome, phase, validated, token_spent) are now read from a
first-line slice of the raw response, `OUTCOME_LINE=${OUTCOME_RAW%%$'\n'*}`, so a
multi-line `OUTCOME_RAW` still classifies correctly from its first line and the
rejection branch runs, while the trailing, deliberately unrestricted `cut -f5-` read
still carries every line of the error into `GATE_ERROR` unchanged. `TestReasonFlatteningIsJSONSafe`
below therefore now drives its PRIMARY fixture with a literal LINE FEED
(`HOSTILE_REFUSAL_LF`); the original carriage-return fixture (`HOSTILE_REFUSAL`) stays
in the same class as a SIBLING case, unedited, rather than being deleted, so the record
of what this suite could and could not prove stays legible.
`TestMultilineRefusalReachesTheRejectionBranch` and its mutation contrast, further down
this file, are the runtime proof that the rejection branch -- not the outage branch --
is the one that actually runs on a multi-line refusal (plan.md ## Capabilities items
1-3), and `TestMultilineRefusalReapproveAdviceFollowsTokenSpent`,
`TestMultilineRefusalTruncationBoundStillApplies` and
`TestMultilineRefusalExitsZeroRegardlessOfTelemetry` extend capabilities 4, 5 and 12 of
the ORIGINAL cycle's claims to the multi-line case this cycle adds. Capability 6 (plan.md:
"the single-line paths do not move") is the existing 27 tests in this file, run
unedited, and no assertion above this point in the file may move to accommodate this
cycle's change.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

# WHICH typed stream a row landed on is part of what this file asserts, so it opts out
# of the autouse WRIT_FRICTION_LOG redirect in tests/conftest.py: that variable
# collapses every stream into one file, which read_streams (what this file reads back
# with) does not honour, and the isolation this file needs instead is per-test
# WRIT_LOG_ROOT / WRIT_LOG_PROJECT under tmp_path (same reasoning as
# tests/test_approval_evidence.py and tests/test_replan_reopen_planning.py).
pytestmark = pytest.mark.no_friction_isolation

SKILL_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = SKILL_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"

from tests.fixtures.session_state import (  # noqa: E402
    sandbox_cwd,  # noqa: F401 -- imported for its autouse cwd-pin side effect
    write_evidence_transcript,
)
from writ.session.gate_token import gate_token_path  # noqa: E402
from writ.session.mode_engine import _gate_sequence_for_mode  # noqa: E402
from writ.shared.logging import read_streams, stream_path  # noqa: E402

# The two approval drivers that reach the exact/override arm without needing the
# session's own transcript evidence machinery re-derived here (plan.md ## Test design:
# "The exact tier needs approval evidence ... the `approved anyway` override phrase
# gives a second driver that needs no transcript"). Each test in this file that mints a
# token is expected to exercise at least one of these two shapes; several run both.
EXACT_APPROVAL_PROMPT = "approved"
OVERRIDE_APPROVAL_PROMPT = "approved anyway"

# A prompt approval_match classifies as the `embedded` tier: a real approval word inside
# a longer sentence, which is the only arm that emits approval_pattern_miss.
EMBEDDED_APPROVAL_PROMPT = "ok remember we want to fix all our findings, approved"

# The log scope every hook run in this file writes under. Named explicitly (rather than
# derived from the sandbox's git identity) so the read-back resolves the same scope the
# write did without either side having to guess, and so `WRIT_LOG_ROOT` under tmp_path
# is the only thing separating one test's rows from another's.
LOG_PROJECT = "approval-reason-probe"

# The stream STREAM_MAP routes approval_pattern_match and approval_pattern_miss to.
FRICTION_STREAM = "friction"

# Plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's own fixture: a refusal whose text is
# genuinely multi-line, with a distinct marker word on each line, so a test can assert
# every line survived the flatten-and-record step rather than only the first.
MULTILINE_REFUSAL = (
    "plan.md validation failed: LINEONEMARKER missing ## Files.\n"
    "LINETWOMARKER missing ## Capabilities.\n"
    "LINETHREEMARKER fix ALL issues in one edit."
)


# ---------------------------------------------------------------------------
# Shared infrastructure. Every function below is a signature and a docstring only
# (SKL-PROC-WRIT-FAILURE-001 skeleton convention): the behavior it promises is what
# the implementation cycle builds, not this cycle.
# ---------------------------------------------------------------------------


def _sid(label: str) -> str:
    """A uuid-suffixed session id namespaced by `label`, so two tests in this file (or
    a leftover from a prior run) can never collide on the same `/tmp/writ-gate-token-*`
    path."""
    return f"approval-reason-{label}-{uuid.uuid4().hex[:8]}"


def _seed_pending_gate(cache_dir: Path, sid: str, gate: str = "phase-a", mode: str = "work") -> None:
    """Seed `<cache_dir>/writ-session-<sid>.json` with `gate` as the sole pending gate
    in `mode`, the minimum state the exact/override arm needs to attempt an advance."""
    sequence = _gate_sequence_for_mode(mode)
    approved = sequence[: sequence.index(gate)] if gate else list(sequence)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Every approved gate is bound to a null plan fingerprint, which is what
    # `plan_md_hash` answers for a project with no plan.md: the binding matches, so the
    # approval still counts and the NEXT gate in the sequence is the pending one. An
    # empty `gate` therefore means "nothing pending at all", the no-advance-attempted
    # state TestOutcomeVocabularyPinnedUnchanged needs.
    (cache_dir / f"writ-session-{sid}.json").write_text(
        json.dumps({
            "mode": mode,
            "current_phase": "planning" if gate else "implementation",
            "gates_approved": approved,
            "gates_approved_plan": {name: None for name in approved},
            "project_root": "",
            "denial_counts": {},
        })
    )


def _isolated_env(
    tmp_path: Path,
    cache_dir: Path,
    fake_home: Path,
    log_project: str,
    daemon_port: int | None,
    extra: dict | None = None,
) -> dict:
    """Build the subprocess environment for one hook run under the isolation contract
    this module's docstring states: WRIT_CACHE_DIR/WRIT_LOG_ROOT/WRIT_LOG_PROJECT/HOME
    all rooted under `tmp_path`; WRIT_SOCKET POPPED from the inherited environment
    (THE ISOLATION TRAP -- not merely left absent from a dict literal, which would
    still let an inherited value through `os.environ` if a caller built the env by
    merging); WRIT_HOST/WRIT_PORT set to `daemon_port` when given, else to a closed
    port so no advance is even attempted over the network."""
    env = dict(os.environ)
    env.pop("WRIT_SOCKET", None)
    env.pop("WRIT_FRICTION_LOG", None)
    fake_home.mkdir(parents=True, exist_ok=True)
    if daemon_port is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            daemon_port = probe.getsockname()[1]
    env.update({
        "HOME": str(fake_home),
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_LOG_ROOT": str(tmp_path / "logs"),
        "WRIT_LOG_PROJECT": log_project,
        "WRIT_HOST": "127.0.0.1",
        "WRIT_PORT": str(daemon_port),
    })
    env.update(extra or {})
    return env


def _run_hook(
    payload: dict, cwd: Path, env: dict, hook_path: Path = HOOK_PATH,
) -> subprocess.CompletedProcess:
    """Run hooks/scripts/auto-approve-gate.sh as a real bash subprocess with `payload`
    JSON-encoded on stdin; return the completed process (stdout, stderr, returncode)
    for the caller to assert on.

    `hook_path` defaults to the real hook and is overridden only by the mutation proof,
    which runs a deliberately damaged COPY through this same runner so both sides of
    that comparison are the real subprocess.

    The envelope carries `agent_id` alongside `session_id` (the hook reads
    `agent_id or session_id`, so the row's identity is unchanged either way) purely to
    take the `[ -z "$AGENT_ID" ]` branch that would otherwise overwrite the machine-wide
    `/tmp/writ-current-session` pointer, which belongs to the operator's live session
    and not to this suite.
    """
    envelope = dict(payload)
    envelope.setdefault("agent_id", envelope.get("session_id", ""))
    return subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(envelope),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_stream_rows(project: str, stream: str) -> list[dict]:
    """Read every row of `stream` back for `project` through
    writ.shared.logging.read_streams, at the destination the router itself resolves
    under the test's own WRIT_LOG_ROOT -- never a path this file assembles by hand."""
    return read_streams(project, [stream])


def _raw_stream_lines(project: str, stream: str) -> list[str]:
    """The RAW (unparsed) lines of `<WRIT_LOG_ROOT>/<project>/<stream>.jsonl`, read via
    writ.shared.logging.stream_path so the destination is the router's own answer, not
    a guessed path. Used where a test must prove the file holds exactly one line, which
    json-parsing alone cannot show (a second, malformed line would silently vanish
    under a parse-and-count)."""
    path = stream_path(project, stream)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


@contextlib.contextmanager
def _mint_cleanup(sid: str):
    """Remove `writ.session.gate_token.gate_token_path(sid)` on exit, success or
    failure, so a test that mints a token never leaks it into a shared /tmp."""
    try:
        yield
    finally:
        try:
            os.remove(gate_token_path(sid))
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leaked_gate_tokens():
    """The confined safety net tests/test_approval_evidence.py and
    tests/test_replan_reopen_planning.py both carry: `confined_leak_sweep` removes --
    and fails this test on -- only the gate-token files whose session id could NOT
    belong to a live human session; everything else is left on disk untouched and
    reported via `warn_about_left_alone`, because this suite cannot tell such a file
    from an approval a human typed in another window."""
    from tests._gate_token_leak import (
        confined_leak_sweep,
        live_snapshot,
        warn_about_left_alone,
    )

    before = live_snapshot()
    yield
    removed, left_alone = confined_leak_sweep(before)
    warn_about_left_alone(left_alone)
    assert not removed, f"test leaked gate token file(s) (now removed): {sorted(removed)}"


class _CountingAdvanceHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the daemon's /session/<id>/advance-phase route: answers every
    request with this class's `body` (a canned JSON response) and increments
    `post_count` on the class itself, so a test can read the count back after the
    server is torn down (THE ISOLATION TRAP's positive signal, not an absence)."""

    body = b'{"error": "plan.md validation failed: missing X. Fix ALL issues in one edit.", "token_spent": true}'
    post_count = 0

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
        """Read and discard the request body, increment post_count, answer with
        `self.__class__.body`."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        self.__class__.post_count += 1
        payload = self.__class__.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args, **_kwargs) -> None:
        """Silence BaseHTTPRequestHandler's default stderr access log."""
        return None


@contextlib.contextmanager
def _mock_advance_daemon(response_body: bytes):
    """A real local http.server standing in for the Writ daemon's advance-phase route
    -- a genuine socket accepting a genuine POST, not a patched python function -- so
    the hook's own curl/urllib client code runs unmodified. Yields
    (port, handler_cls) so a test can assert `handler_cls.post_count` after the `with`
    block exits."""
    handler_cls = type(
        "_CountedHandler", (_CountingAdvanceHandler,), {"body": response_body, "post_count": 0},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler_cls
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _hook_source_miss_emit_sites(source: str) -> list[int]:
    """Every line number in `source` (the hook script's text) where an
    `approval_pattern_miss` event is built for `log_friction_event`, DERIVED from the
    source rather than a hardcoded count -- so a future second emit site is caught by
    construction, and this detector's population can never silently go stale."""
    sites = []
    for number, line in enumerate(source.splitlines(), start=1):
        if "log_friction_event" in line and "approval_pattern_miss" in line:
            sites.append(number)
    return sites


def _miss_extra_variable_at(source: str, line_number: int) -> str:
    """The shell variable a miss emit site passes as its EXTRA-fields argument
    (`"$MISS_EXTRA"` -> `MISS_EXTRA`), so the per-site assertion can go and read how
    THAT variable is built instead of assuming every site shares one name."""
    line = source.splitlines()[line_number - 1]
    match = re.search(r'"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"\s*$', line.strip())
    return match.group(1) if match else ""


def _miss_row_tier(
    tmp_path: Path, log_project: str, sid: str, hook_path: Path = HOOK_PATH,
) -> str | None:
    """Trigger the embedded-tier path for `sid` and return the resulting
    `approval_pattern_miss` row's `tier` field (None if the row or the field is
    absent). The runtime half of the source-derived detector: the source scan in
    `_hook_source_miss_emit_sites` proves WHERE a miss is logged, this proves WHAT the
    row it produces actually carries.

    `tmp_path` and `hook_path` are parameters because the runtime half needs a place to
    put the isolated cache/home/project the hook reads, and because the mutation proof
    must run this same detector against a damaged COPY of the hook.
    """
    cache_dir = tmp_path / f"cache-{sid}"
    fake_home = tmp_path / f"home-{sid}"
    project = tmp_path / f"project-{sid}"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _seed_pending_gate(cache_dir, sid)
    # No daemon: the embedded arm asks and advances nothing, so it never opens a socket.
    _run_hook(
        {"session_id": sid, "prompt": EMBEDDED_APPROVAL_PROMPT},
        cwd=project,
        env=_isolated_env(tmp_path, cache_dir, fake_home, log_project, None),
        hook_path=hook_path,
    )
    rows = [
        row for row in _read_stream_rows(log_project, FRICTION_STREAM)
        if row.get("event") == "approval_pattern_miss" and row.get("session") == sid
    ]
    return rows[-1].get("tier") if rows else None


def _mutate_hook_removing_tier_field(tmp_path: Path) -> Path:
    """Write a copy of the real hook into `tmp_path` with the `'tier': 'embedded'` key
    deleted from the miss row's MISS_EXTRA construction; return the mutated copy's
    path. The positive proof that this cycle's miss-tier detector is conditional: a
    detector run against this mutant must fail where it passes against the real hook.

    The copy sits at `<root>/hooks/scripts/<name>` with `bin` and `writ` symlinked
    beside it, because the hook derives WRIT_DIR from its own location and sources
    common.sh from there: a copy dropped anywhere else would die at the source line and
    the mutant would then fail the detector for the wrong reason.
    """
    root = tmp_path / "mutant-skill"
    (root / "hooks" / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("bin", "writ"):
        link = root / name
        if not link.exists():
            link.symlink_to(SKILL_ROOT / name)
    source = HOOK_PATH.read_text(encoding="utf-8")
    mutated = source.replace(", 'tier': 'embedded'", "")
    assert mutated != source, (
        "the mutation changed nothing: the hook source no longer carries "
        "\", 'tier': 'embedded'\", so this mutant proves nothing about the detector"
    )
    target = root / "hooks" / "scripts" / HOOK_PATH.name
    target.write_text(mutated, encoding="utf-8")
    return target


def _match_rows(log_project: str, sid: str) -> list[dict]:
    """Every approval_pattern_match row this session wrote, read back through the
    router. One expression rather than four lines repeated in every test."""
    return [
        row for row in _read_stream_rows(log_project, FRICTION_STREAM)
        if row.get("event") == "approval_pattern_match" and row.get("session") == sid
    ]


def _drive_approval(
    tmp_path: Path,
    sid: str,
    response_body: bytes | None,
    prompt: str = EXACT_APPROVAL_PROMPT,
    gate: str = "phase-a",
    env_extra: dict | None = None,
    hook_path: Path = HOOK_PATH,
) -> tuple[subprocess.CompletedProcess, type | None]:
    """One real approval turn: seed the pending gate, stand up the canned daemon when
    `response_body` is given, and run the real hook against it under `_isolated_env`.

    Returns the completed process and the stand-in handler class (None when no daemon
    was stood up), so a caller can read `post_count` off the handler itself rather than
    off bookkeeping of its own.

    `hook_path` defaults to the real hook and is overridden only by the mutation
    contrast, for the reason `_run_hook(hook_path=)` gives: both sides of that
    comparison must be the real subprocess driven by the same runner.
    """
    cache_dir = tmp_path / f"cache-{sid}"
    fake_home = tmp_path / f"home-{sid}"
    project = tmp_path / f"project-{sid}"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _seed_pending_gate(cache_dir, sid, gate=gate)
    payload = {"session_id": sid, "prompt": prompt}
    if prompt == EXACT_APPROVAL_PROMPT:
        payload["transcript_path"] = write_evidence_transcript(tmp_path)
    if response_body is None:
        env = _isolated_env(tmp_path, cache_dir, fake_home, LOG_PROJECT, None, env_extra)
        return _run_hook(payload, cwd=project, env=env, hook_path=hook_path), None
    with _mock_advance_daemon(response_body) as (port, handler_cls):
        env = _isolated_env(tmp_path, cache_dir, fake_home, LOG_PROJECT, port, env_extra)
        proc = _run_hook(payload, cwd=project, env=env, hook_path=hook_path)
    return proc, handler_cls


# ---------------------------------------------------------------------------
# Capabilities 1-2: `reason` is conditional on rejection, proved by its positive
# counterpart (an advance that succeeds) in the SAME class.
# ---------------------------------------------------------------------------


class TestReasonFieldIsConditionalOnRejection:
    """A rejected advance's `approval_pattern_match` row carries `reason` = the
    server's refusal text while `outcome` stays exactly the string `rejected`; a
    successful advance in the SAME class writes a row with `outcome`
    `advanced-><phase>` and NO `reason` key at all (plan.md ## Capabilities items 1-2,
    explicitly paired: "proved by its positive counterpart in the same class").

    PASSES VACUOUSLY IF: the rejection half ran alone -- a row builder that writes NO
    fields, ever, would pass a bare `'reason' in row` check on nothing and prove
    nothing about conditionality without the advanced-case sibling below.
    """

    def test_rejected_row_carries_the_servers_refusal_text_and_outcome_stays_rejected(
        self, tmp_path,
    ) -> None:
        refusal = (
            "plan.md validation failed: missing ## Files; missing ## Capabilities. "
            "Fix ALL issues in one edit."
        )
        sid = _sid("rejected-reason")
        body = json.dumps({"error": refusal, "token_spent": True}).encode()
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, body)
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0]["outcome"] == "rejected", (
            f"the outcome string must stay byte-identical: {rows[0]['outcome']!r}"
        )
        assert rows[0].get("reason") == refusal, (
            "the row must persist the server's refusal text verbatim; got "
            f"{rows[0].get('reason')!r}"
        )

    def test_advanced_row_carries_outcome_advanced_arrow_phase_and_no_reason_key(
        self, tmp_path,
    ) -> None:
        sid = _sid("advanced-no-reason")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, b'{"phase": "test-skeletons"}')
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0]["outcome"] == "advanced->test-skeletons", (
            f"the advanced outcome string must stay byte-identical: {rows[0]['outcome']!r}"
        )
        assert "reason" not in rows[0], (
            f"an advance that succeeded has no refusal to record: {rows[0]!r}"
        )


# ---------------------------------------------------------------------------
# Capabilities 3-4: token_spent is a true JSON tri-state.
# ---------------------------------------------------------------------------


class TestTokenSpentTriState:
    """`token_spent` is recorded as JSON `true` when the server reports the approval
    was consumed, JSON `false` when it reports it was not, and the KEY IS OMITTED
    (never `null`, never the string `"true"`/`"false"`) when the server did not report
    it at all -- so "we do not know" stays distinguishable from "not spent" (plan.md
    ## Capabilities items 3-4). The reporting-response case lives in this same class as
    the omission case, per plan.md's explicit "with a reporting response in the same
    test [class] as the positive control".

    PASSES VACUOUSLY IF: the omission case ran with no reporting-response sibling in
    this class -- a builder that always omits the key would pass the omission check
    while never actually implementing the tri-state.
    """

    @pytest.mark.parametrize(
        "advance_response_body,expected_token_spent",
        [
            (
                b'{"error": "plan.md validation failed: missing X. Fix ALL issues in one edit.", '
                b'"token_spent": true}',
                True,
            ),
            (
                b'{"error": "no resolvable project root", "token_spent": false}',
                False,
            ),
        ],
    )
    def test_token_spent_is_recorded_as_a_real_json_boolean(
        self, tmp_path, advance_response_body, expected_token_spent,
    ) -> None:
        sid = _sid("token-spent-bool")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, advance_response_body)
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0].get("token_spent") is expected_token_spent, (
            "token_spent must be a real JSON boolean, never the string 'true'/'false'; "
            f"got {rows[0].get('token_spent')!r}"
        )

    def test_token_spent_key_is_omitted_when_the_server_response_has_no_token_spent_field(
        self, tmp_path,
    ) -> None:
        sid = _sid("token-spent-silent")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, b'{"error": "plan.md validation failed: missing X."}')
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        # The reason IS recorded on this same row, so the missing key below is the
        # builder deciding, not the builder never running.
        assert rows[0].get("reason") == "plan.md validation failed: missing X."
        assert "token_spent" not in rows[0], (
            "a server that did not report token_spent must leave the key ABSENT, so "
            f"'we do not know' stays distinct from 'not spent': {rows[0]!r}"
        )

    def test_positive_control_token_spent_key_is_present_when_the_server_does_report_it(
        self, tmp_path,
    ) -> None:
        sid = _sid("token-spent-reported")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": false}',
            )
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert "token_spent" in rows[0], (
            "the same refusal text that omits the key above must CARRY it when the "
            f"server reports it, or the omission proves nothing: {rows[0]!r}"
        )
        assert rows[0]["token_spent"] is False


# ---------------------------------------------------------------------------
# Capability 5: the 300-character bound, prefix-truncated, short text stored whole.
# ---------------------------------------------------------------------------


class TestReasonTruncationBound:
    """A refusal text longer than 300 characters is stored truncated to exactly 300
    and is a PREFIX of the text the server returned; a refusal text under the bound is
    stored whole, byte for byte (plan.md ## Capabilities item 5; ## Analysis: 300, not
    120, because `writ/session/approval_workflow.py:198` joins a multi-issue refusal
    into one `'; '.join(missing)` list, and 120 would cut it after the first item).

    PASSES VACUOUSLY IF: only the long case ran -- a builder that always truncates to
    300 characters (padding a short reason to fill it) would pass the long case and
    silently corrupt every short one, which the second test below is what would catch.
    """

    def test_a_refusal_text_over_the_bound_is_stored_truncated_to_300_chars_as_a_prefix(
        self, tmp_path,
    ) -> None:
        refusal = "plan.md validation failed: " + ("missing section; " * 30) + "TAILMARKER"
        assert len(refusal) > 300, "the fixture must exceed the bound to test the bound"
        sid = _sid("bound-long")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, json.dumps({"error": refusal}).encode())
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0].get("reason")
        assert stored == refusal[:300], (
            f"a long refusal must be stored as its own 300-char prefix; got {stored!r}"
        )
        assert len(stored) == 300
        assert "TAILMARKER" not in stored

    @pytest.mark.parametrize("length,expect_truncated", [(300, False), (301, True)])
    def test_the_bound_is_exercised_at_its_own_edge(
        self, tmp_path, length, expect_truncated,
    ) -> None:
        """THE EDGE, not just well above and well below it. A bound tested only at
        27 and 578 characters is satisfied by an off-by-one slice; at exactly 300 the
        text must survive whole, and one character above it must lose exactly one."""
        head = "plan.md validation failed: "
        refusal = head + ("x" * (length - len(head)))
        assert len(refusal) == length
        sid = _sid(f"bound-edge-{length}")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, json.dumps({"error": refusal}).encode())
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0].get("reason")
        assert len(stored) == 300
        assert stored == refusal[:300]
        assert (stored != refusal) is expect_truncated, (
            f"at {length} chars the stored reason should "
            f"{'differ from' if expect_truncated else 'equal'} the server text"
        )

    def test_a_refusal_text_under_the_bound_is_stored_whole_and_unpadded(
        self, tmp_path,
    ) -> None:
        refusal = "no resolvable project root"
        sid = _sid("bound-short")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, json.dumps({"error": refusal}).encode())
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0].get("reason")
        assert stored == refusal, (
            f"a short refusal must be stored whole, byte for byte; got {stored!r}"
        )
        assert len(stored) == len(refusal) < 300


# ---------------------------------------------------------------------------
# Capability 6: the security-shaped case -- a multi-line, quoted, backslashed reason
# must not be able to forge a second JSON row.
# ---------------------------------------------------------------------------


class TestReasonFlatteningIsJSONSafe:
    """A refusal text containing quotes, backslashes, tabs and embedded CR/LF produces
    EXACTLY ONE parseable JSON row whose `reason` field contains no raw CR or LF and
    still carries a recognizable word from every line of the original multi-line
    server message (plan.md ## Capabilities item 6, the security-shaped case: "A
    multi-line server message must not be able to forge a second JSON row").

    PASSES VACUOUSLY IF: the row were read via `json.loads` on one hand-picked line
    instead of asserting the RAW stream file holds exactly one line total -- a builder
    that let a raw LF through would silently write two JSON lines, and a test that
    only ever parses `lines[0]` would never see the second one appear.

    BOTH LINE BREAKS ARE DRIVEN HERE, as of plan.md 2412ba38's follow-up cycle. A raw
    LF now DOES reach `GATE_ERROR`, because the hook's four fixed classifier fields are
    read from a first-line slice of the raw response and no longer smear a multi-line
    error into the verdict comparison; `HOSTILE_REFUSAL_LF` below is the PRIMARY
    fixture for that reason (plan.md ## Capabilities item 11). `HOSTILE_REFUSAL` (the
    carriage-return fixture) stays as the SIBLING case this class already had, rather
    than being deleted: it is the other half of the same log-forging vector the
    router's `_sanitize_value` names (SEC-INJ-LOG-001), and the two tests built on it
    below are unedited. The paragraph this replaces recorded the LF limit as
    permanent; it was measured and correct under the hook as it stood then, and is
    superseded, not erased, by this rewrite.
    """

    HOSTILE_REFUSAL = (
        'plan.md validation failed: ALPHAMARKER\rBETAMARKER "quoted" \\ back\tslash'
    )

    # The PRIMARY fixture for capability 11: a literal LINE FEED, not only a carriage
    # return, carrying the same hostile shape (quotes, backslash, tab) as HOSTILE_REFUSAL.
    #
    # WHY THE TAB SITS BEFORE THE BREAK HERE, while HOSTILE_REFUSAL keeps it after.
    # The two were first written to differ in exactly one character, and that is what
    # exposed a limit this cycle does not own: a CR makes no record line, but an LF does,
    # so a tab AFTER the LF lands on the record's SECOND line, where `cut -f5-` (applied
    # per line) sees only two fields and drops BETAMARKER, the quotes and the backslash.
    # Measured byte-exactly. That is the "known limit" plan.md states rather than works
    # around: changing the trailing read is a separate cycle. Keeping the tab after the
    # break would make this fixture assert the limit instead of the flattening it exists
    # to prove, so the tab moves and the reason is written down rather than the
    # assertion weakened.
    HOSTILE_REFUSAL_LF = (
        'plan.md validation failed: ALPHAMARKER back\tslash\nBETAMARKER "quoted" \\'
    )

    def test_a_multiline_quoted_backslashed_refusal_produces_exactly_one_raw_json_line(
        self, tmp_path,
    ) -> None:
        sid = _sid("forge-one-line")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid, json.dumps({"error": self.HOSTILE_REFUSAL}).encode(),
            )
        lines = _raw_stream_lines(LOG_PROJECT, FRICTION_STREAM)
        assert len(lines) == 1, (
            "a hostile refusal must not be able to forge a second row in the stream "
            f"file; the file holds {len(lines)} line(s): {lines!r}"
        )
        row = json.loads(lines[0])
        assert row["event"] == "approval_pattern_match"
        assert row["outcome"] == "rejected"
        assert row.get("reason"), f"the single row must still carry the reason: {row!r}"

    def test_the_stored_reason_carries_no_raw_cr_or_lf_and_keeps_words_from_every_line(
        self, tmp_path,
    ) -> None:
        sid = _sid("forge-flattened")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid, json.dumps({"error": self.HOSTILE_REFUSAL}).encode(),
            )
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0]["reason"]
        assert "\r" not in stored and "\n" not in stored and "\t" not in stored, (
            f"the stored reason must carry no raw CR, LF or tab: {stored!r}"
        )
        # Split on whitespace, not a substring search: a router that merely DELETED the
        # control characters would glue the two markers into one token, and a substring
        # check could not tell that apart from the flattening this cycle adds.
        assert {"ALPHAMARKER", "BETAMARKER"} <= set(stored.split()), (
            "every segment of the original message must survive as its own word: "
            f"{stored!r}"
        )
        assert '"quoted"' in stored and "\\" in stored, (
            f"quotes and backslashes must survive the flattening intact: {stored!r}"
        )

    def test_a_multiline_lf_refusal_produces_exactly_one_raw_json_line(
        self, tmp_path,
    ) -> None:
        """Capability 11's own fixture: a HOSTILE_REFUSAL_LF (literal LINE FEED, not
        only carriage return) must not be able to forge a second row either, now that
        it reaches GATE_ERROR at all -- the same one-line-per-row proof
        `test_a_multiline_quoted_backslashed_refusal_produces_exactly_one_raw_json_line`
        gives for the CR fixture above, run against the LF sibling."""
        sid = _sid("forge-one-line-lf")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid, json.dumps({"error": self.HOSTILE_REFUSAL_LF}).encode(),
            )
        lines = _raw_stream_lines(LOG_PROJECT, FRICTION_STREAM)
        assert len(lines) == 1, (
            "a refusal carrying a literal line feed must not be able to forge a second "
            f"row in the stream file; the file holds {len(lines)} line(s): {lines!r}"
        )
        row = json.loads(lines[0])
        assert row["event"] == "approval_pattern_match"
        assert row["outcome"] == "rejected", (
            "a literal line feed must still reach the rejection branch, not the outage "
            f"branch: {row!r}"
        )
        assert row.get("reason"), f"the single row must still carry the reason: {row!r}"

    def test_the_stored_lf_reason_carries_no_raw_cr_lf_or_tab_and_keeps_words_from_every_line(
        self, tmp_path,
    ) -> None:
        """Capability 11: no raw CR, LF or tab in `reason`, and every marker
        (ALPHAMARKER, BETAMARKER) survives as its own word -- the same proof
        `test_the_stored_reason_carries_no_raw_cr_or_lf_and_keeps_words_from_every_line`
        gives for the CR fixture, run against HOSTILE_REFUSAL_LF."""
        sid = _sid("forge-flattened-lf")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid, json.dumps({"error": self.HOSTILE_REFUSAL_LF}).encode(),
            )
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0]["reason"]
        assert "\r" not in stored and "\n" not in stored and "\t" not in stored, (
            f"the stored reason must carry no raw CR, LF or tab: {stored!r}"
        )
        # Split on whitespace, not a substring search, for the reason the CR sibling
        # above gives: a router that merely DELETED the line feed would glue the two
        # markers into one token and a substring check could not tell that apart.
        assert {"ALPHAMARKER", "BETAMARKER"} <= set(stored.split()), (
            "every segment of the original message must survive as its own word: "
            f"{stored!r}. MEASURED CAUSE if this is red: HOSTILE_REFUSAL_LF puts its "
            "tab AFTER the line feed, so the record's second line carries two fields "
            "and the trailing `cut -f5-` (which applies to every line, and which this "
            "cycle keeps byte-identical) yields nothing for it. That is the limit "
            "plan.md names under 'A known limit, stated rather than worked around'; a "
            "later line with NO tab survives whole (MULTILINE_REFUSAL proves that in "
            "TestMultilineRefusalReachesTheRejectionBranch). Either the fixture's tab "
            "moves onto line one or the trailing read changes, and the second is out "
            "of scope for this cycle."
        )
        assert '"quoted"' in stored and "\\" in stored, (
            f"quotes and backslashes must survive the flattening intact: {stored!r}"
        )


# ---------------------------------------------------------------------------
# Capability 7: gate behavior is unchanged -- stdout, minting, exit code.
# ---------------------------------------------------------------------------


class TestGateBehaviorIsUnchanged:
    """For the same canned rejection, the hook still prints the REJECTED line carrying
    the FULL, UNTRUNCATED reason (the 300-char bound applies only inside the row
    builder, never to `GATE_ERROR`/stdout, per ## Analysis: "one variable stays one
    variable"), still gives the correct re-approve advice for both `token_spent`
    states, still mints the gate token file, and still exits 0 (plan.md ##
    Capabilities item 7).

    PASSES VACUOUSLY IF: a SHORT reason were used to drive the stdout assertion -- a
    wrongly-truncated GATE_ERROR would still print something plausible-looking under
    300 characters; only a reason LONGER than the bound can show stdout keeping the
    full text while the row keeps the truncated one.
    """

    LONG_REFUSAL = "plan.md validation failed: " + ("missing section; " * 30) + "TAILMARKER"

    def test_stdout_prints_the_rejected_line_with_the_full_untruncated_reason(
        self, tmp_path,
    ) -> None:
        sid = _sid("stdout-full-reason")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid, json.dumps({"error": self.LONG_REFUSAL}).encode(),
            )
        assert "gate REJECTED, not advanced" in proc.stdout, (
            f"the REJECTED line must still be printed: {proc.stdout!r}"
        )
        assert self.LONG_REFUSAL in proc.stdout, (
            "stdout must carry the FULL refusal text; the 300-char bound belongs to "
            f"the row builder alone: {proc.stdout!r}"
        )
        rows = _match_rows(LOG_PROJECT, sid)
        assert rows[0]["reason"] == self.LONG_REFUSAL[:300], (
            "the row is the bounded copy, in the same run where stdout kept the whole "
            f"text: {rows[0].get('reason')!r}"
        )

    def test_stdout_advises_reapproval_is_required_when_token_spent_is_true(
        self, tmp_path,
    ) -> None:
        sid = _sid("stdout-spent-true")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
        assert (
            "the rejection spent the prior approval, so the user must approve again"
            in proc.stdout
        ), f"a spent token must still tell the user to approve again: {proc.stdout!r}"
        assert "Your approval was NOT consumed" not in proc.stdout

    def test_stdout_advises_no_reapproval_needed_when_token_spent_is_false(
        self, tmp_path,
    ) -> None:
        sid = _sid("stdout-spent-false")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                b'{"error": "no resolvable project root", "token_spent": false}',
            )
        assert "Your approval was NOT consumed" in proc.stdout, (
            f"an unspent token must still say so: {proc.stdout!r}"
        )
        assert "you do not need to approve again" in proc.stdout

    def test_the_gate_token_file_is_still_minted_on_a_rejection(self, tmp_path) -> None:
        sid = _sid("mint-on-rejection")
        token_path = gate_token_path(sid)
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
            assert os.path.exists(token_path), (
                f"the approval must still mint its bound token file at {token_path}"
            )
            lines = Path(token_path).read_text(encoding="utf-8").splitlines()
        assert lines[0], "line 1 (the secret) must be non-empty"
        assert lines[1] == "phase-a", (
            f"line 2 must still bind the token to the pending gate: {lines!r}"
        )

    def test_the_hook_still_exits_zero_on_a_rejection(self, tmp_path) -> None:
        sid = _sid("exit-zero")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
        assert proc.returncode == 0, (
            f"a UserPromptSubmit hook must never block the turn: {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Capability 8: telemetry failure cannot fail the hook.
# ---------------------------------------------------------------------------


class TestTelemetryFailureCannotFailTheHook:
    """With the friction writer forced to fail -- WRIT_LOG_ROOT pointed at a path that
    cannot hold a project subdirectory, a REAL OSError surfacing from the REAL router
    rather than a mocked failure, per ENF-SYS-005 -- the hook still exits 0, still
    prints the reason to the user on stdout, and no row lands at the normal
    destination (plan.md ## Capabilities item 8; ## Analysis: "the row builder still
    runs as `... 2>/dev/null | ... 2>/dev/null || true`").

    PASSES VACUOUSLY IF: "no row is written" were established by never checking (an
    absent assertion looks identical to a passing one) rather than by a POSITIVE read
    through `_read_stream_rows` that returns an empty list at the same destination
    another test in this file proves IS reachable when telemetry is healthy.
    """

    REFUSAL = "plan.md validation failed: missing ## Capabilities."

    def _broken_log_root(self, tmp_path: Path) -> dict:
        """A WRIT_LOG_ROOT that is a regular FILE, so the router's
        `path.parent.mkdir(...)` raises a genuine NotADirectoryError."""
        broken = tmp_path / "broken-log-root"
        broken.write_text("this is a file, not a directory", encoding="utf-8")
        return {"WRIT_LOG_ROOT": str(broken)}

    def test_hook_exits_zero_when_the_friction_writer_cannot_write(self, tmp_path) -> None:
        sid = _sid("telemetry-exit")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                json.dumps({"error": self.REFUSAL, "token_spent": True}).encode(),
                env_extra=self._broken_log_root(tmp_path),
            )
        assert proc.returncode == 0, (
            f"telemetry failure must never fail the hook: {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )

    def test_hook_still_prints_the_reason_to_the_user_when_the_friction_writer_cannot_write(
        self, tmp_path,
    ) -> None:
        sid = _sid("telemetry-stdout")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                json.dumps({"error": self.REFUSAL, "token_spent": True}).encode(),
                env_extra=self._broken_log_root(tmp_path),
            )
        assert self.REFUSAL in proc.stdout, (
            "the in-turn message is what the user acts on, and it must survive a dead "
            f"log writer: {proc.stdout!r}"
        )

    def test_no_row_lands_at_the_normal_destination_when_the_friction_writer_cannot_write(
        self, tmp_path,
    ) -> None:
        sid = _sid("telemetry-norow")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid,
                json.dumps({"error": self.REFUSAL, "token_spent": True}).encode(),
                env_extra=self._broken_log_root(tmp_path),
            )
        # Read at the HEALTHY destination every other test in this file writes to and
        # reads from, so the emptiness is a measured absence at a reachable path.
        assert _match_rows(LOG_PROJECT, sid) == [], (
            "a failed write must not somehow land at the normal destination anyway"
        )


# ---------------------------------------------------------------------------
# Capabilities 9-10: the miss row's `tier` field, source-derived and mutation-proved.
# ---------------------------------------------------------------------------


class TestMissRowCarriesEmbeddedTier:
    """Every `approval_pattern_miss` emit found in the hook SOURCE carries `tier` set
    to `embedded`, with the population derived from `_hook_source_miss_emit_sites`
    (a scan of the hook's text) rather than a hardcoded list or count (plan.md ##
    Capabilities item 9; ## Analysis: "`classify()` has five outcomes ... and any
    future arm that logs the same event would silently merge into the same
    population").

    PASSES VACUOUSLY IF: the source-derived population were allowed to be empty and
    the per-site loop below still reported green on zero iterations -- closed by the
    non-empty assertion and by TestMissTierDetectorConditionalByMutation's mutation
    proof that this class's detector actually discriminates.
    """

    def test_the_source_derived_population_of_miss_emit_sites_is_non_empty(self) -> None:
        sites = _hook_source_miss_emit_sites(HOOK_PATH.read_text(encoding="utf-8"))
        assert sites, (
            "no approval_pattern_miss emit site was found in the hook source: the "
            "detector behind capability 9 would then be measuring nothing"
        )

    def test_every_discovered_miss_emit_site_writes_tier_embedded_at_runtime(
        self, tmp_path,
    ) -> None:
        source = HOOK_PATH.read_text(encoding="utf-8")
        sites = _hook_source_miss_emit_sites(source)
        assert sites
        for line_number in sites:
            variable = _miss_extra_variable_at(source, line_number)
            assert variable, (
                f"miss emit at line {line_number} passes no named extra-fields "
                "variable, so this detector cannot say what the row it writes carries"
            )
            assert re.search(rf"^\s*{variable}=.*'tier': 'embedded'", source, re.MULTILINE), (
                f"the extra fields ${variable} feeding the miss emit at line "
                f"{line_number} are not built with 'tier': 'embedded'"
            )
        sid = _sid("miss-tier")
        assert _miss_row_tier(tmp_path, LOG_PROJECT, sid) == "embedded", (
            "the row the embedded arm actually writes must carry tier=embedded, not "
            "merely look right in the source"
        )


class TestMissTierDetectorConditionalByMutation:
    """The detector behind `TestMissRowCarriesEmbeddedTier` is proved conditional by
    mutation: a copy of the hook in `tmp_path` with the `tier` field deleted from
    MISS_EXTRA must FAIL the same detector the real hook passes (plan.md ##
    Capabilities item 10; ## Test design: "the mutant must fail the detector").

    PASSES VACUOUSLY IF: only the mutant were exercised -- a detector that always
    returns "fail" would pass this test alone and prove nothing; the positive control
    is `TestMissRowCarriesEmbeddedTier::test_every_discovered_miss_emit_site_writes_tier_embedded_at_runtime`,
    which must pass against the real, unmutated hook for this test to mean anything.
    """

    def test_a_hook_copy_with_the_tier_field_removed_fails_the_miss_tier_detector(
        self, tmp_path,
    ) -> None:
        mutant = _mutate_hook_removing_tier_field(tmp_path)
        sid = _sid("mutant-miss")
        tier = _miss_row_tier(tmp_path, LOG_PROJECT, sid, hook_path=mutant)
        # The mutant must fail for ITS reason: the row is still written, and it is the
        # tier FIELD that is gone. Without this the detector would also "fail" on a
        # mutant that simply died before logging anything.
        mutant_rows = [
            row for row in _read_stream_rows(LOG_PROJECT, FRICTION_STREAM)
            if row.get("event") == "approval_pattern_miss" and row.get("session") == sid
        ]
        assert mutant_rows, (
            "the mutant wrote no approval_pattern_miss row at all, so this proves "
            "nothing about the tier field; the damaged copy did not run"
        )
        assert tier is None, (
            f"the mutant must fail the detector the real hook passes; got {tier!r}"
        )
        mutant_source = mutant.read_text(encoding="utf-8")
        for line_number in _hook_source_miss_emit_sites(mutant_source):
            variable = _miss_extra_variable_at(mutant_source, line_number)
            assert not re.search(
                rf"^\s*{variable}=.*'tier': 'embedded'", mutant_source, re.MULTILINE
            ), "the source half of the detector must fail on the mutant too"


# ---------------------------------------------------------------------------
# Capability 11: every hook subprocess in this file is provably isolated from the
# operator's real daemon.
# ---------------------------------------------------------------------------


class TestDaemonIsolationIsAPositiveSignal:
    """The canned daemon stand-in records exactly one POST for one approval, and the
    row this test reads back comes from the destination `writ.shared.logging` itself
    resolves under the test's own WRIT_LOG_ROOT -- never a path this file assembled by
    hand (plan.md ## Capabilities item 11; THE ISOLATION TRAP in this module's
    docstring).

    PASSES VACUOUSLY IF: the POST count were read from this test's own bookkeeping
    (e.g. a counter incremented by the test's calling code) instead of the handler's
    independently-maintained `post_count`, or if the row read-back used a path this
    file assembled instead of `read_streams`/`stream_path` -- exactly the isolation
    failure this class exists to catch (see this module's docstring on the 182-record
    production-graph incident this pattern is descended from).
    """

    def test_the_stand_in_daemon_receives_exactly_one_post_for_one_approval(
        self, tmp_path,
    ) -> None:
        sid = _sid("one-post")
        with _mint_cleanup(sid):
            _, handler_cls = _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
        assert handler_cls.post_count == 1, (
            "the stand-in's own counter is the positive signal that this approval went "
            f"to THIS server and not the operator's daemon; got {handler_cls.post_count}"
        )

    def test_the_row_is_read_back_from_the_destination_the_router_itself_resolves(
        self, tmp_path,
    ) -> None:
        sid = _sid("router-destination")
        with _mint_cleanup(sid):
            _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
        from writ.shared.state_root import default_log_root

        resolved = stream_path(LOG_PROJECT, FRICTION_STREAM)
        assert tmp_path in resolved.parents, (
            f"the router must resolve this test's own log root: {resolved}"
        )
        assert Path(default_log_root()) not in resolved.parents, (
            f"no row in this suite may resolve to the install's real log root: {resolved}"
        )
        assert _match_rows(LOG_PROJECT, sid), (
            f"the row must be readable at the destination the router resolves: {resolved}"
        )

    def test_no_writ_socket_env_var_reaches_the_hook_subprocess(
        self, tmp_path, monkeypatch,
    ) -> None:
        # The trap needs the variable PRESENT in the inherited environment: popping it
        # from an environment that never had it would prove nothing.
        monkeypatch.setenv("WRIT_SOCKET", str(tmp_path / "decoy.sock"))
        assert "WRIT_SOCKET" in os.environ
        env = _isolated_env(
            tmp_path, tmp_path / "cache-probe", tmp_path / "home-probe", LOG_PROJECT, 1,
        )
        assert "WRIT_SOCKET" not in env, (
            "WRIT_SOCKET must be POPPED from the subprocess environment, or curl "
            "ignores WRIT_HOST/WRIT_PORT and POSTs to the operator's real daemon"
        )
        sid = _sid("socket-popped")
        with _mint_cleanup(sid):
            _, handler_cls = _drive_approval(
                tmp_path, sid,
                b'{"error": "plan.md validation failed: missing X.", "token_spent": true}',
            )
        assert handler_cls.post_count == 1, (
            "with WRIT_SOCKET set in the parent environment, the approval must still "
            f"reach the stand-in over TCP; got {handler_cls.post_count} POST(s)"
        )


# ---------------------------------------------------------------------------
# Non-negotiable, cross-cutting: the outcome vocabulary itself must not move.
# ---------------------------------------------------------------------------


class TestOutcomeVocabularyPinnedUnchanged:
    """`outcome` still selects exactly `advanced-><phase>`, `rejected`, or the fallback
    `ask-prompt-emitted` -- BYTE-IDENTICAL to the strings on disk today, because
    existing analyzers count them (plan.md's "OTHER NON-NEGOTIABLES": "The outcome
    strings ... must be pinned as UNCHANGED"). This class is deliberately separate from
    the field-presence classes above: a `reason`/`token_spent` regression could leave
    `outcome` untouched, and an `outcome` regression could leave the new fields intact,
    so the two must fail independently.

    PASSES VACUOUSLY IF: the three outcome strings were asserted with `in` against a
    longer message instead of exact string equality on the `outcome` field itself, which
    would still pass if a future change renamed the field's CONTENTS while keeping some
    substring recognizable.
    """

    @pytest.mark.parametrize(
        "advance_response_body,expected_outcome",
        [
            (b'{"phase": "test-skeletons"}', "advanced->test-skeletons"),
            (
                b'{"error": "plan.md validation failed: missing X. Fix ALL issues in one edit."}',
                "rejected",
            ),
        ],
    )
    def test_outcome_for_advanced_and_rejected_is_pinned_exactly(
        self, tmp_path, advance_response_body, expected_outcome,
    ) -> None:
        sid = _sid("vocabulary")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, advance_response_body)
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0]["outcome"] == expected_outcome, (
            f"outcome must be exactly {expected_outcome!r}; got {rows[0]['outcome']!r}"
        )

    def test_outcome_falls_back_to_ask_prompt_emitted_when_no_advance_is_attempted(
        self, tmp_path,
    ) -> None:
        sid = _sid("vocabulary-fallback")
        # No gate pending, so the exact arm never opens a socket: the row is written
        # anyway, and it is the fallback string it must carry.
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, None, gate="")
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0]["outcome"] == "ask-prompt-emitted", (
            f"the fallback string must stay byte-identical: {rows[0]['outcome']!r}"
        )
        assert "reason" not in rows[0] and "token_spent" not in rows[0], (
            "a turn where nothing was ever asked of the server must claim neither a "
            f"refusal nor a token state: {rows[0]!r}"
        )


# =============================================================================
# Plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's follow-up cycle: "a multi-line
# refusal must reach the rejection branch, not the outage branch". Everything below
# this banner is new for that cycle (## Capabilities items 1-6, 11, 12). Capability 6
# (the single-line regression fleet) is every test ABOVE this banner, run unedited; it
# has no new test of its own here, per the plan's non-negotiable: "Do not edit an
# existing assertion to accommodate the change; if one would have to move, that is a
# finding, not an edit." Capabilities 7-10 (the derived cut-site detector) live in
# tests/test_tab_record_cut_guard.py, not here.
#
# Every helper and test body below was `raise NotImplementedError` when this file was
# approved (TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001 skeleton convention); the
# implementation cycle filled them in against the hook change plan.md's ## Analysis names.
# =============================================================================


def _mutate_hook_restoring_prefix_read(tmp_path: Path) -> Path:
    """Write a copy of the real (post-fix) hook into `tmp_path` with the fixed-field
    reads reverted to the PRE-FIX shape: `OUTCOME` (and, for completeness, the other
    three fixed fields) read directly off `$OUTCOME_RAW` rather than the first-line
    slice `$OUTCOME_LINE`, so `cut -f1` again applies to every line of a multi-line
    refusal instead of just the first; return the mutated copy's path.

    The negative control for `TestMultilineRefusalReachesTheRejectionBranch`: this
    mutant must reproduce the exact outage behavior that class proves the real, unmutated
    hook no longer has, through the SAME runner (`_run_hook`) both sides of the contrast
    use. Same symlinked `bin`/`writ` layout `_mutate_hook_removing_tier_field` already
    uses and for the same reason: the hook derives `WRIT_DIR` from its own location and
    sources `common.sh` from there, so a copy dropped anywhere else dies at the source
    line and the mutant then fails for the wrong reason.

    Must assert the mutation actually changed the hook's text (mirroring
    `_mutate_hook_removing_tier_field`'s own assertion), or a hook already reverted to
    the pre-fix shape (mid-implementation, or before the fix lands at all) would make
    this helper's caller pass for having proven nothing.
    """
    root = tmp_path / "prefix-mutant-skill"
    (root / "hooks" / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("bin", "writ"):
        link = root / name
        if not link.exists():
            link.symlink_to(SKILL_ROOT / name)
    source = HOOK_PATH.read_text(encoding="utf-8")
    mutated = source.replace('"$OUTCOME_LINE" | cut', '"$OUTCOME_RAW" | cut')
    assert mutated != source, (
        "the mutation changed nothing: the hook source no longer reads its fixed "
        "fields from \"$OUTCOME_LINE\", so this mutant proves nothing about the "
        "first-line slice"
    )
    target = root / "hooks" / "scripts" / HOOK_PATH.name
    target.write_text(mutated, encoding="utf-8")
    return target


def _broken_log_root_env(tmp_path: Path) -> dict:
    """A `WRIT_LOG_ROOT` override pointed at a path that is a regular FILE rather than a
    directory, so the router's `path.parent.mkdir(...)` raises a genuine
    `NotADirectoryError` -- the same real-failure shape
    `TestTelemetryFailureCannotFailTheHook._broken_log_root` already builds for the
    single-line case, reproduced here as a standalone module-level helper so this
    file's new capability-12 class does not have to reach into a sibling class's
    private method."""
    broken = tmp_path / "broken-log-root"
    broken.write_text("this is a file, not a directory", encoding="utf-8")
    return {"WRIT_LOG_ROOT": str(broken)}


class TestMultilineRefusalReachesTheRejectionBranch:
    """THE DEFECT THIS CLASS PINS (plan.md 2412ba38 ## Analysis): a refusal carrying a
    literal line feed made `$OUTCOME` -- read, before this fix, with an unguarded
    `cut -f1` over the WHOLE multi-line classifier output -- itself multi-line, so
    `[ "$OUTCOME" = "rejected" ]` was false, `GATE_ERROR` stayed empty, and the turn
    fell through to the daemon-did-not-answer arm while the row recorded
    `ask-prompt-emitted` with no reason at all. This class drives the real hook against
    `MULTILINE_REFUSAL` and asserts the rejection branch is the one that actually runs
    (plan.md ## Capabilities items 1-2).

    PASSES VACUOUSLY IF: only stdout were checked and the row read-back skipped (or
    vice versa) -- a hook that prints the rejection but still logs the outage row, or
    logs the rejection but still prints the outage text, would satisfy half of this
    class and fail nothing on the other half; both halves are asserted here.
    """

    RESPONSE = json.dumps({"error": MULTILINE_REFUSAL, "token_spent": True}).encode()

    def test_stdout_prints_the_rejected_line_carrying_every_line_of_the_message(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-stdout")
        with _mint_cleanup(sid):
            proc, handler_cls = _drive_approval(tmp_path, sid, self.RESPONSE)
        assert handler_cls.post_count == 1, (
            "the approval must have reached THIS stand-in, or the text below says "
            f"nothing about what a refusal does; got {handler_cls.post_count} POST(s)"
        )
        assert "gate REJECTED, not advanced" in proc.stdout, (
            "a refusal carrying a literal line feed must reach the rejection branch: "
            f"{proc.stdout!r}"
        )
        for line in MULTILINE_REFUSAL.split("\n"):
            assert line in proc.stdout, (
                f"the printed refusal is missing the line {line!r}: {proc.stdout!r}"
            )
        assert MULTILINE_REFUSAL in proc.stdout, (
            f"stdout must carry the whole message, unbroken: {proc.stdout!r}"
        )

    def test_stdout_prints_none_of_the_daemon_did_not_answer_outage_text(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-no-outage")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(tmp_path, sid, self.RESPONSE)
        assert "the Writ daemon did not answer" not in proc.stdout, (
            "the daemon answered and refused; reporting an outage sends the user to "
            f"restart a healthy daemon and hides the reason: {proc.stdout!r}"
        )
        assert "systemctl --user restart writ-server" not in proc.stdout, proc.stdout
        assert "Your approval was not consumed." not in proc.stdout, (
            "the outage arm's claim about the token must not be printed for a refusal "
            f"the server answered: {proc.stdout!r}"
        )

    def test_exactly_one_row_is_written_with_outcome_rejected_and_a_non_empty_reason(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-row")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, self.RESPONSE)
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0]["outcome"] == "rejected", (
            "a multi-line refusal is a refusal, not an unanswered ask: "
            f"{rows[0]['outcome']!r}"
        )
        assert rows[0].get("reason"), (
            f"the row must record WHY the advance was refused: {rows[0]!r}"
        )

    def test_the_rows_reason_carries_a_word_from_every_line_of_the_message(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-reason-words")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, self.RESPONSE)
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0]["reason"]
        # As words, not substrings: a reason that merely DELETED the line feeds would
        # glue the last word of one line to the first of the next.
        assert {"LINEONEMARKER", "LINETWOMARKER", "LINETHREEMARKER"} <= set(stored.split()), (
            f"every line of the server's message must survive in the row: {stored!r}"
        )
        assert "\n" not in stored and "\r" not in stored and "\t" not in stored, stored


class TestMultilineRefusalMutationContrastProvesTheOutageWasTheOldBehavior:
    """Capability 3: the claim above is proved CONDITIONAL by mutation, in this same
    class -- a copy of the hook with the pre-fix read restored (field 1 taken from the
    WHOLE classifier output, not the first-line slice) answers the identical
    `MULTILINE_REFUSAL` with the outage text and a row whose `outcome` is
    `ask-prompt-emitted` and which has no `reason` key. Mirrors the house pattern
    `TestMissTierDetectorConditionalByMutation` already carries for the miss-tier
    detector: the positive proof lives in the sibling class above, and this class is
    only the negative control.

    PASSES VACUOUSLY IF: the mutant's own row were never checked for having WRITTEN
    anything at all -- a mutant that crashed before logging would also show no
    `rejected` row and no `reason` key, and would pass every assertion here while
    proving nothing about the specific pre-fix READ this class exists to contrast
    against.
    """

    RESPONSE = json.dumps({"error": MULTILINE_REFUSAL, "token_spent": True}).encode()

    def test_the_mutation_actually_changed_the_hook_source(self, tmp_path) -> None:
        mutant = _mutate_hook_restoring_prefix_read(tmp_path)
        mutated = mutant.read_text(encoding="utf-8")
        real = HOOK_PATH.read_text(encoding="utf-8")
        assert mutated != real, "the mutant is a copy of the real hook and proves nothing"
        assert 'OUTCOME=$(printf \'%s\' "$OUTCOME_RAW" | cut -f1)' in mutated, (
            "the mutant must carry the PRE-FIX read, field 1 taken from the whole "
            "classifier output"
        )
        assert '"$OUTCOME_LINE" | cut' not in mutated, (
            "no fixed read may still go through the first-line slice, or the contrast "
            "is against a half-mutated hook"
        )
        # The slice itself stays assigned, so the mutant differs from the real hook in
        # WHICH VALUE the fixed reads take and in nothing else.
        assert "OUTCOME_LINE=${OUTCOME_RAW%%$'\\n'*}" in mutated

    def test_the_prefix_mutant_answers_the_multiline_refusal_with_the_outage_text(
        self, tmp_path,
    ) -> None:
        mutant = _mutate_hook_restoring_prefix_read(tmp_path)
        sid = _sid("mutant-multiline-stdout")
        with _mint_cleanup(sid):
            proc, handler_cls = _drive_approval(
                tmp_path, sid, self.RESPONSE, hook_path=mutant,
            )
        assert handler_cls.post_count == 1, (
            "the mutant must have ASKED the stand-in and been refused; without that it "
            f"reports an outage for the right reason: {handler_cls.post_count} POST(s)"
        )
        assert "the Writ daemon did not answer" in proc.stdout, (
            "the pre-fix read is what turned a refusal into an outage report; if this "
            f"is absent the contrast proves nothing: {proc.stdout!r}"
        )
        assert "gate REJECTED, not advanced" not in proc.stdout, proc.stdout
        assert "LINETWOMARKER" not in proc.stdout, (
            f"the pre-fix hook dropped the refusal text entirely: {proc.stdout!r}"
        )
        assert proc.returncode == 0, proc.stderr

    def test_the_prefix_mutant_writes_a_row_with_outcome_ask_prompt_emitted_and_no_reason_key(
        self, tmp_path,
    ) -> None:
        mutant = _mutate_hook_restoring_prefix_read(tmp_path)
        sid = _sid("mutant-multiline-row")
        with _mint_cleanup(sid):
            _drive_approval(tmp_path, sid, self.RESPONSE, hook_path=mutant)
        rows = _match_rows(LOG_PROJECT, sid)
        # FOR ITS REASON: the mutant still WROTE a row. A copy that died before logging
        # would show no rejected row and no reason key too, and would pass this class
        # while saying nothing about the read it was made to restore.
        assert len(rows) == 1, (
            "the mutant wrote no approval_pattern_match row at all, so this proves "
            f"nothing about the pre-fix read; the damaged copy did not run: {rows!r}"
        )
        assert rows[0]["outcome"] == "ask-prompt-emitted", (
            "the measured pre-fix behavior: a refusal misfiled as an unanswered ask; "
            f"got {rows[0]['outcome']!r}"
        )
        assert "reason" not in rows[0], (
            f"the pre-fix row recorded no reason at all: {rows[0]!r}"
        )


class TestMultilineRefusalReapproveAdviceFollowsTokenSpent:
    """Capability 4: the re-approve advice is still decided by the server's
    `token_spent` flag on a MULTI-LINE refusal, exactly as
    `TestGateBehaviorIsUnchanged` already proves for the single-line case -- the spent
    wording appears when the response reports `token_spent` true and the
    not-consumed wording when it reports false.

    PASSES VACUOUSLY IF: only one of the two `token_spent` states were driven --
    a hook that always printed the "spent" wording regardless of the flag would pass a
    lone `true` case and fail nothing.
    """

    @pytest.mark.parametrize(
        "token_spent,expected_phrase,forbidden_phrase",
        [
            (
                True,
                "the rejection spent the prior approval, so the user must approve again",
                "Your approval was NOT consumed",
            ),
            (
                False,
                "Your approval was NOT consumed",
                "the rejection spent the prior approval, so the user must approve again",
            ),
        ],
    )
    def test_reapprove_advice_follows_the_servers_token_spent_flag_on_a_multiline_refusal(
        self, tmp_path, token_spent, expected_phrase, forbidden_phrase,
    ) -> None:
        sid = _sid(f"multiline-spent-{str(token_spent).lower()}")
        body = json.dumps({"error": MULTILINE_REFUSAL, "token_spent": token_spent}).encode()
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(tmp_path, sid, body)
        assert expected_phrase in proc.stdout, (
            f"a multi-line refusal reporting token_spent={token_spent} must advise "
            f"{expected_phrase!r}: {proc.stdout!r}"
        )
        assert forbidden_phrase not in proc.stdout, (
            f"the opposite advice must not appear: {proc.stdout!r}"
        )
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        assert rows[0].get("token_spent") is token_spent, (
            "the row must record the same flag the advice was decided from: "
            f"{rows[0].get('token_spent')!r}"
        )


class TestMultilineRefusalTruncationBoundStillApplies:
    """Capability 5: for a multi-line refusal LONGER than the 300-character bound, the
    hook still prints the full, untruncated text on stdout while the row's `reason` is
    still its 300-character prefix -- the same "one variable stays one variable" claim
    `TestGateBehaviorIsUnchanged` proves for a single-line refusal, now proved for a
    refusal whose length crosses multiple lines.

    PASSES VACUOUSLY IF: the fixture used were under the 300-character bound -- a
    wrongly-truncated `GATE_ERROR` would still print something plausible-looking short
    of that length; only a refusal LONGER than the bound can show stdout keeping the
    full multi-line text while the row keeps the truncated one.
    """

    LONG_MULTILINE_REFUSAL = (
        "plan.md validation failed: LINEONE ALPHAMARKER\n"
        + ("missing section; " * 20)
        + "\nLINETHREE OMEGAMARKER fix ALL issues in one edit."
    )

    def test_stdout_keeps_the_full_multiline_text_while_the_rows_reason_is_its_300_char_prefix(
        self, tmp_path,
    ) -> None:
        assert len(self.LONG_MULTILINE_REFUSAL) > 300, (
            "the fixture must exceed the bound to test the bound"
        )
        sid = _sid("multiline-bound")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid,
                json.dumps({"error": self.LONG_MULTILINE_REFUSAL}).encode(),
            )
        assert self.LONG_MULTILINE_REFUSAL in proc.stdout, (
            "stdout must carry the FULL multi-line refusal; the 300-character bound "
            f"belongs to the row builder alone: {proc.stdout!r}"
        )
        assert "OMEGAMARKER" in proc.stdout, (
            f"the last line must survive on stdout: {proc.stdout!r}"
        )
        rows = _match_rows(LOG_PROJECT, sid)
        assert len(rows) == 1, f"expected exactly one approval_pattern_match row: {rows!r}"
        stored = rows[0]["reason"]
        flattened = self.LONG_MULTILINE_REFUSAL.replace("\n", " ").strip()
        assert stored == flattened[:300], (
            "the row is the flattened, bounded copy of the same text stdout kept "
            f"whole: {stored!r}"
        )
        assert len(stored) == 300
        assert "ALPHAMARKER" in stored and "OMEGAMARKER" not in stored, (
            f"the bound must cut the tail, not the head: {stored!r}"
        )


class TestMultilineRefusalExitsZeroRegardlessOfTelemetry:
    """Capability 12: the hook exits 0 on a multi-line refusal whether or not the
    friction writer can write, mirroring `TestTelemetryFailureCannotFailTheHook`'s
    single-line proof of the same claim (plan.md ## Analysis, "Failure behavior.
    Unchanged... the parameter expansion cannot fail under `set -u`").

    PASSES VACUOUSLY IF: only the telemetry-healthy case were driven -- a hook that
    exits 0 only when the friction writer succeeds would pass that case alone and hide
    a regression in the `2>/dev/null ... || true` failure-swallowing chain this class's
    second test exists to catch.
    """

    RESPONSE = json.dumps({"error": MULTILINE_REFUSAL, "token_spent": True}).encode()

    def test_the_hook_exits_zero_on_the_multiline_refusal_with_telemetry_healthy(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-exit-healthy")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(tmp_path, sid, self.RESPONSE)
        assert proc.returncode == 0, (
            f"a UserPromptSubmit hook must never block the turn: {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )
        assert _match_rows(LOG_PROJECT, sid), (
            "telemetry was healthy in this run, so the row must be there: the exit code "
            "above is then the exit code of a hook that did the work"
        )

    def test_the_hook_exits_zero_on_the_multiline_refusal_when_the_friction_writer_cannot_write(
        self, tmp_path,
    ) -> None:
        sid = _sid("multiline-exit-broken")
        with _mint_cleanup(sid):
            proc, _ = _drive_approval(
                tmp_path, sid, self.RESPONSE,
                env_extra=_broken_log_root_env(tmp_path),
            )
        assert proc.returncode == 0, (
            f"telemetry failure must never fail the hook: {proc.returncode}, "
            f"stderr={proc.stderr!r}"
        )
        assert "gate REJECTED, not advanced" in proc.stdout, (
            "the in-turn message is what the user acts on, and it must survive a dead "
            f"log writer: {proc.stdout!r}"
        )
        for line in MULTILINE_REFUSAL.split("\n"):
            assert line in proc.stdout, proc.stdout
