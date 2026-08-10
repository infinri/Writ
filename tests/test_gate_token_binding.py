"""tests/test_gate_token_binding.py

Cycle 1 (plan.md finding 4): the gate token records what it authorizes -- the gate and
the plan fingerprint -- instead of being a bare secret any pending gate will accept. A
token minted for one purpose could otherwise be replayed against whatever gate happened
to be pending when it was consumed (a plan approval spending itself on a decision-memory
candidate promotion, or a stale token from a re-armed session advancing the wrong gate).

Design decisions this file's tests commit to (none of these are stated verbatim in
plan.md by name; they are the narrowest reading of its Analysis section, corrected once
after review -- see below):

  * `mint_gate_token(session_id, *, gate, plan_hash, token=None)` writes the three-line
    file and returns the token (a fresh one unless `token=` overrides it -- the override
    exists so a test can drive the SAME token through two independent writers and compare
    bytes, per capability 6).
  * The bare rename-mutex (today's whole `claim_gate_token` body: rename, read, remove,
    compare the WHOLE file content against `supplied_token`) is kept as its own internal
    primitive, `_claim_token_mutex(session_id, supplied_token)`, UNCHANGED in behavior.
    tests/test_advance_phase_token_claim.py's four pre-existing tests point at that name
    now, with their original two-positional-argument call, and are not part of this
    cycle's red set.
  * The public `claim_gate_token(session_id, supplied_token, *, gate, plan_hash)` has
    `gate` and `plan_hash` keyword-only and REQUIRED -- no default. An earlier version of
    this file gave them `=None` defaults meaning "do not enforce this half of the
    binding," which review correctly rejected: a fail-open default on the function that
    decides whether a human approved an action means a caller that forgets the argument
    gets an unguarded claim and no error, silently reopening the hole this cycle exists
    to close. Making them required turns "forgot to pass the binding" into a TypeError
    at the call site instead of a silent bypass -- pinned below by
    TestClaimRequiresBindingArguments. The now-required arguments are never satisfied
    with a sentinel like `""` standing in for "don't check": a caller that genuinely has
    no gate pending passes the literal empty string `gate=""` (what mint_gate_token wrote
    for that state), which IS enforced -- it just happens to enforce "must be exactly
    empty."
  * The bash side of the byte-parity contract (capability 6) is a shared function,
    `write_gate_token_file <path> <token> <gate> <plan_hash>`, added to bin/lib/common.sh
    -- the same file that already owns `log_friction_event` and `writ_http_post`, the
    other primitives auto-approve-gate.sh shares with the rest of the hook surface.

RED today: mint_gate_token, `_claim_token_mutex`, the gate=/plan_hash= REQUIRED keyword
parameters on `claim_gate_token`, write_gate_token_file, and the next_gate/plan_hash keys
on cmd_current_phase's JSON all do not exist yet. Each class below names the specific
symbol whose absence makes it fail.

Per ENF-SYS-005, TestConcurrentClaimsRealProcesses drives the claim primitive from real
OS child processes against a real file in a real temp directory -- nothing here is
proven with a mock, and the claim above about "exactly one wins" would not be provable
any other way (a mocked os.rename tells you what you told it to return).

Cleanup discipline: gate_token_path() hardcodes /tmp deliberately (its own docstring:
the bash writer and the python reader must never be able to disagree on where the file
lives), so unlike the session cache there is no WRIT_CACHE_DIR a test can redirect this
into tmp_path. Every test that mints wraps the risky section in `_mint_cleanup(sid)`,
which removes the file in a `finally` even when the test body raises -- that is the
PRIMARY mechanism. The autouse `_no_leaked_gate_tokens` fixture below is the safety net:
it snapshots /tmp/writ-gate-token-* before and after every test in this module, removes
anything new that is still present, and fails the test if there was anything to remove,
so a test that forgot its own cleanup is caught here rather than leaking a file into the
real /tmp for the life of the machine (where a collision with a real session id would
forge an approval).

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
"""

from __future__ import annotations

import contextlib
import glob
import json
import multiprocessing
import os
import subprocess
import uuid
from pathlib import Path

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
COMMON_SH = os.path.join(SKILL_ROOT, "bin", "lib", "common.sh")

# A plan.md shape already proven to pass approval_workflow._validate_phase_a (the same
# text tests/test_pol6f_approval_workflow_extraction.py uses), parameterized on the one
# capability checkbox so callers can flip [ ] <-> [x] without touching anything else --
# that one edit is exactly what locators.plan_md_hash's checkbox normalization exists
# to make a non-event (capability 9).
_PLAN_TEMPLATE = (
    "# Plan\n## Files\n- a.py\n## Analysis\nwhy\n## Rules Applied\nNo matching rules.\n"
    "## Capabilities\n- {box} does the thing\n"
)


def _sid(label: str) -> str:
    return f"gtb-{label}-{uuid.uuid4().hex[:8]}"


def _write_plan(tmp_path, ticked: bool = False) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text(_PLAN_TEMPLATE.format(box="[x]" if ticked else "[ ]"))
    return plan


def _call_json(fn, *args) -> dict:
    """Capture one function's stdout JSON, per tests/test_escalation_reset_on_advance.py."""
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return json.loads(buf.getvalue().strip())


def _call_advance_json(monkeypatch, fn, *args) -> dict:
    """_call_json's counterpart for cmd_advance_phase specifically: that function
    unconditionally does a bare `sys.stdin.read()` (it tolerates a piped prompt), which
    pytest's default output capture replaces with an object that raises OSError on
    read() -- see tests/test_pol6f_approval_workflow_extraction.py's `_advance` helper,
    which stubs stdin for the same reason. Without this, a test whose token happens to
    pass the line-1 match reaches that read() and fails on a capture-plumbing OSError
    instead of the real assertion below it.
    """
    import io as _io

    monkeypatch.setattr("sys.stdin", _io.StringIO(""))
    return _call_json(fn, *args)


@contextlib.contextmanager
def _mint_cleanup(sid: str):
    """Guarantees /tmp/writ-gate-token-<sid> is removed after the block, even when the
    block raises. This is the PRIMARY cleanup mechanism every test that mints uses; the
    autouse _no_leaked_gate_tokens fixture below is the safety net that turns a
    forgotten instance of this into a loud test failure instead of a silent /tmp leak.
    """
    from writ.session.gate_token import gate_token_path

    try:
        yield
    finally:
        try:
            os.remove(gate_token_path(sid))
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leaked_gate_tokens():
    """Safety net, not the primary mechanism (see `_mint_cleanup`): every gate-token
    test in this module is covered by this fixture even if it forgets its own cleanup.
    Removes anything new found after the test and FAILS the test if there was anything
    to remove -- a leaked file in the real /tmp is not a cosmetic issue: a later
    collision with a real Claude Code session id would let that stray file be read as
    an approval it never received.
    """
    before = set(glob.glob("/tmp/writ-gate-token-*"))
    yield
    after = set(glob.glob("/tmp/writ-gate-token-*"))
    leaked = after - before
    for path in leaked:
        try:
            os.remove(path)
        except OSError:
            pass
    assert not leaked, f"test leaked gate token file(s) (now removed): {sorted(leaked)}"


# ---------------------------------------------------------------------------
# Capability 6: token file format + bash/python writer byte parity
# ---------------------------------------------------------------------------


class TestTokenFileFormat:
    """Line 1 token, line 2 bound gate, line 3 plan fingerprint."""

    def test_mint_gate_token_writes_three_lines(self):
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("format")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")
            with open(gate_token_path(sid)) as f:
                lines = f.read().split("\n")
            assert lines[0] == token
            assert lines[1] == "phase-a"
            assert lines[2] == "abc123def456"

    def test_mint_gate_token_with_no_gate_pending_writes_an_empty_second_line(self):
        """The state a promote-candidate-only approval mints: no phase gate pending."""
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("nogate")
        with _mint_cleanup(sid):
            mint_gate_token(sid, gate="", plan_hash="")
            with open(gate_token_path(sid)) as f:
                lines = f.read().split("\n")
            assert lines[1] == ""
            assert lines[2] == ""

    def test_mint_gate_token_returns_the_token_it_wrote(self):
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("returns")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="test-skeletons", plan_hash="deadbeef0000")
            with open(gate_token_path(sid)) as f:
                on_disk_first_line = f.read().split("\n")[0]
            assert token == on_disk_first_line
            assert token, "mint_gate_token must not return an empty token"

    def test_bash_writer_and_mint_gate_token_are_byte_identical(self, tmp_path, monkeypatch):
        """write_gate_token_file (bin/lib/common.sh) is the bash side of the same
        contract. Both writers are driven with the SAME explicit token
        (mint_gate_token's `token=` override exists precisely so this comparison is not
        reconciling two independent random generators) and redirected to plain tmp_path
        files via a monkeypatched gate_token_path, so this never touches the real /tmp
        for a real session id and needs no _mint_cleanup.
        """
        import writ.session.gate_token as gt

        sid = _sid("parity")
        fixed_token = uuid.uuid4().hex
        py_path = tmp_path / "py-token"
        bash_path = tmp_path / "bash-token"

        monkeypatch.setattr(gt, "gate_token_path", lambda session_id: str(py_path))
        gt.mint_gate_token(sid, gate="phase-a", plan_hash="0123456789ab", token=fixed_token)

        script = (
            f'set -euo pipefail\nsource "{COMMON_SH}"\n'
            f'write_gate_token_file "{bash_path}" "{fixed_token}" "phase-a" "0123456789ab"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, f"write_gate_token_file failed: {r.stderr}"

        py_bytes = py_path.read_bytes()
        bash_bytes = bash_path.read_bytes()
        assert py_bytes == bash_bytes, f"python bytes: {py_bytes!r}\nbash bytes: {bash_bytes!r}"


# ---------------------------------------------------------------------------
# Capability 7: read_gate_token stays line-1-only
# ---------------------------------------------------------------------------


class TestReadGateTokenLineOneOnly:
    def test_read_gate_token_returns_exactly_the_minted_token(self):
        from writ.session.gate_token import mint_gate_token, read_gate_token

        sid = _sid("readline1")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")
            assert read_gate_token(sid) == token

    def test_read_gate_token_on_a_bound_file_leaks_no_binding_text(self):
        """Regression guard for the presence checks at gate.py:67/310 and the CLI's
        expected_token read: they compare read_gate_token()'s return against a caller-
        supplied token, so any binding text leaking onto line 1 breaks every match."""
        from writ.session.gate_token import mint_gate_token, read_gate_token

        sid = _sid("noleak")
        with _mint_cleanup(sid):
            mint_gate_token(sid, gate="test-skeletons", plan_hash="0123456789ab")
            result = read_gate_token(sid)
            assert "\n" not in result
            assert "test-skeletons" not in result
            assert "0123456789ab" not in result


# ---------------------------------------------------------------------------
# claim_gate_token's binding arguments are required, not fail-open defaults
# ---------------------------------------------------------------------------


class TestClaimRequiresBindingArguments:
    """gate and plan_hash are keyword-only and REQUIRED on the public claim_gate_token
    -- no default. Omitting either must raise TypeError at the call site rather than
    silently skipping enforcement: a None (or any other sentinel) default would be a
    fail-open bypass on the function that decides whether a human approved an action,
    just a greppable one.

    test_omitting_both_raises_type_error is the one case actually red against today's
    code: calling with exactly two positional arguments matches TODAY's (pre-cycle)
    claim_gate_token(session_id, supplied_token) signature exactly, so today it runs
    normally (returns False, no exception) and this test fails because pytest.raises
    caught nothing.

    test_omitting_gate_raises_type_error and test_omitting_plan_hash_raises_type_error
    are NOT red today for the reason this class exists to prove: today's function
    accepts no keyword arguments at all, so passing plan_hash= or gate= already raises
    TypeError('unexpected keyword argument'), coincidentally satisfying
    pytest.raises(TypeError) for the wrong reason. They will keep passing once
    claim_gate_token gains the required keyword-only parameters, at which point the
    SAME assertion is true for the intended reason ('missing 1 required keyword-only
    argument'). Disclosed rather than hidden: an assertion that already passes today
    is not evidence the property holds post-implementation, but pytest.raises(TypeError)
    cannot distinguish the two TypeErrors without pinning CPython's exact message text,
    which would make this test brittle for no real gain -- the case that matters
    (omitting BOTH, the shape a careless caller most plausibly writes) is genuinely red.
    """

    def test_omitting_gate_raises_type_error(self):
        from writ.session.gate_token import claim_gate_token

        with pytest.raises(TypeError):
            claim_gate_token("gtb-typeerror-nogate", "any-token", plan_hash="abc123def456")

    def test_omitting_plan_hash_raises_type_error(self):
        from writ.session.gate_token import claim_gate_token

        with pytest.raises(TypeError):
            claim_gate_token("gtb-typeerror-noplanhash", "any-token", gate="phase-a")

    def test_omitting_both_raises_type_error(self):
        from writ.session.gate_token import claim_gate_token

        with pytest.raises(TypeError):
            claim_gate_token("gtb-typeerror-neither", "any-token")


# ---------------------------------------------------------------------------
# Capability 8: gate-mismatch refusal
# ---------------------------------------------------------------------------


class TestClaimRefusesGateMismatch:
    def test_claim_with_a_different_requested_gate_is_refused(self):
        from writ.session.gate_token import claim_gate_token, mint_gate_token

        sid = _sid("gatemismatch")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")
            claimed = claim_gate_token(sid, token, gate="test-skeletons", plan_hash="abc123def456")
            assert claimed is False

    def test_claim_with_the_matching_gate_succeeds(self):
        from writ.session.gate_token import claim_gate_token, mint_gate_token

        sid = _sid("gatematch")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")
            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash="abc123def456")
            assert claimed is True

    def test_a_token_minted_with_no_gate_pending_refuses_a_phase_advance_claim(self):
        """The promote-candidate-shaped token (gate="") must not double as a
        phase-advance token -- the one deliberate narrowing the plan calls out. Note
        plan_hash="" here, matching the mint exactly: the CORRECT way to express "this
        half is not being exercised" is to pass the real bound value, never a skip
        sentinel (see this file's module docstring)."""
        from writ.session.gate_token import claim_gate_token, mint_gate_token

        sid = _sid("nogateclaim")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="")
            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash="")
            assert claimed is False

    def test_the_cli_advance_names_the_gate_mismatch_as_its_reason(self, tmp_path, monkeypatch):
        """End to end: cmd_advance_phase's JSON reason must say WHY, not just that the
        advance failed -- capability 14's diagnosability requirement, observed here at
        the one gate/reason pairing capability 8 names explicitly."""
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_advance_phase
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import mint_gate_token
        from writ.session.locators import plan_md_hash

        sid = _sid("cliname")
        with _mint_cleanup(sid):
            cache = _read_cache(sid)
            cache.update({"mode": "work", "current_phase": "planning", "gates_approved": []})
            _write_cache(sid, cache)
            _write_plan(tmp_path)
            current_hash = plan_md_hash(str(tmp_path))
            # Bound to a DIFFERENT gate than the one actually pending (phase-a), with the
            # CORRECT plan hash, so a gate mismatch is the only reason this claim can fail.
            token = mint_gate_token(sid, gate="test-skeletons", plan_hash=current_hash)

            result = _call_advance_json(monkeypatch, cmd_advance_phase, sid, str(tmp_path), token)
            assert result["advanced"] is False
            assert "gate" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Capability 9: plan-fingerprint drift
# ---------------------------------------------------------------------------


class TestClaimRefusesPlanDrift:
    def test_claim_is_refused_when_the_plan_hash_no_longer_matches(self):
        from writ.session.gate_token import claim_gate_token, mint_gate_token

        sid = _sid("plandrift")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="hash-at-mint-time")
            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash="hash-at-claim-time")
            assert claimed is False

    def test_a_checkbox_only_edit_between_mint_and_claim_is_accepted(self, tmp_path):
        """locators.plan_md_hash normalizes checkbox tick state, so ticking a box after
        mint (the documented way to close out a cycle) must not look like a pivot."""
        from writ.session.gate_token import claim_gate_token, mint_gate_token
        from writ.session.locators import plan_md_hash

        sid = _sid("checkboxonly")
        with _mint_cleanup(sid):
            _write_plan(tmp_path, ticked=False)
            hash_at_mint = plan_md_hash(str(tmp_path))
            token = mint_gate_token(sid, gate="phase-a", plan_hash=hash_at_mint)

            _write_plan(tmp_path, ticked=True)  # only the capability box's [ ] -> [x] changes
            hash_at_claim = plan_md_hash(str(tmp_path))
            assert hash_at_claim == hash_at_mint, "fixture drift: this edit must normalize to the SAME hash"

            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash=hash_at_claim)
            assert claimed is True

    def test_a_substantive_edit_between_mint_and_claim_is_refused(self, tmp_path):
        from writ.session.gate_token import claim_gate_token, mint_gate_token
        from writ.session.locators import plan_md_hash

        sid = _sid("substantive")
        with _mint_cleanup(sid):
            plan_path = _write_plan(tmp_path, ticked=False)
            hash_at_mint = plan_md_hash(str(tmp_path))
            token = mint_gate_token(sid, gate="phase-a", plan_hash=hash_at_mint)

            plan_path.write_text(plan_path.read_text() + "\n- [ ] a second capability, added after mint\n")
            hash_at_claim = plan_md_hash(str(tmp_path))
            assert hash_at_claim != hash_at_mint, "fixture drift: adding a line must change the hash"

            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash=hash_at_claim)
            assert claimed is False


# ---------------------------------------------------------------------------
# Capability 10: legacy / unbound token file
# ---------------------------------------------------------------------------


class TestClaimRefusesUnboundLegacyToken:
    def test_a_one_line_token_file_is_refused(self):
        """The pre-cycle format: exactly what auto-approve-gate.sh wrote before this
        cycle (`secrets.token_hex(16)` piped straight into the file, nothing else)."""
        from writ.session.gate_token import claim_gate_token, gate_token_path

        sid = _sid("legacy")
        with _mint_cleanup(sid):
            token = uuid.uuid4().hex
            with open(gate_token_path(sid), "w") as f:
                f.write(token + "\n")

            claimed = claim_gate_token(sid, token, gate="phase-a", plan_hash="anything")
            assert claimed is False

    def test_the_cli_tells_the_user_to_approve_again_not_that_no_gate_is_pending(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_advance_phase
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import gate_token_path

        sid = _sid("legacymsg")
        with _mint_cleanup(sid):
            cache = _read_cache(sid)
            cache.update({"mode": "work", "current_phase": "planning", "gates_approved": []})
            _write_cache(sid, cache)
            _write_plan(tmp_path)
            token = uuid.uuid4().hex
            with open(gate_token_path(sid), "w") as f:
                f.write(token + "\n")

            result = _call_advance_json(monkeypatch, cmd_advance_phase, sid, str(tmp_path), token)
            assert result["advanced"] is False
            reason = result["reason"].lower()
            assert "approve again" in reason or "re-approve" in reason or "reapprove" in reason
            assert "no pending gate" not in reason and "no gate is pending" not in reason


# ---------------------------------------------------------------------------
# Capability 11: concurrent claims, real processes (ENF-SYS-005)
# ---------------------------------------------------------------------------


def _claim_worker(sid: str, token: str, gate: str, plan_hash: str, barrier, result_queue) -> None:
    """Runs in a CHILD PROCESS. os.rename's atomicity guarantee is a cross-process
    guarantee, and ENF-SYS-005 requires proving the claim primitive against real
    concurrent OS processes racing a real file, not threads in one process sharing a
    GIL or a mocked rename call."""
    from writ.session.gate_token import claim_gate_token

    barrier.wait()
    result_queue.put(claim_gate_token(sid, token, gate=gate, plan_hash=plan_hash))


class TestConcurrentClaimsRealProcesses:
    def test_exactly_one_of_several_real_process_claims_wins(self):
        from writ.session.gate_token import mint_gate_token

        sid = _sid("realproc")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")

            n = 6
            ctx = multiprocessing.get_context("fork")
            barrier = ctx.Barrier(n)
            result_queue = ctx.Queue()
            procs = [
                ctx.Process(
                    target=_claim_worker,
                    args=(sid, token, "phase-a", "abc123def456", barrier, result_queue),
                )
                for _ in range(n)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=15)

            exit_codes = [p.exitcode for p in procs]
            assert all(code == 0 for code in exit_codes), f"a worker process exited abnormally: {exit_codes}"

            outcomes = [result_queue.get(timeout=5) for _ in range(n)]
            assert outcomes.count(True) == 1, f"exactly one real-process claim must win; got {outcomes}"
            assert outcomes.count(False) == n - 1


# ---------------------------------------------------------------------------
# Capability 12: promote-candidate route binding
# ---------------------------------------------------------------------------


class TestPromoteCandidateBinding:
    """/session/{id}/promote-candidate must refuse a token bound to a pending phase
    gate (the cross-action confusion the plan calls out: a plan approval spent to
    promote a decision-memory candidate) and accept one minted with gate="" (no phase
    gate pending)."""

    @pytest.mark.asyncio
    async def test_a_token_bound_to_a_phase_gate_is_refused(self, monkeypatch):
        import writ.server as server_module
        from writ.server import SessionPromoteCandidateRequest
        from writ.server.routes.gate import session_promote_candidate
        from writ.session.gate_token import mint_gate_token

        monkeypatch.setattr(server_module, "_db", object())
        monkeypatch.setattr(server_module, "_pipeline", object())

        sid = _sid("promote-refuse")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456")

            result = await session_promote_candidate(
                sid, SessionPromoteCandidateRequest(candidate_id="cand-1", token=token)
            )
            assert result.get("promoted") is False
            assert "gate" in (result.get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_a_token_minted_with_no_phase_gate_pending_is_accepted(self, monkeypatch):
        import writ.promotion as promotion_module
        import writ.server as server_module
        from writ.server import SessionPromoteCandidateRequest
        from writ.server.routes.gate import session_promote_candidate
        from writ.session.gate_token import mint_gate_token

        async def _stub_promote(*_args, **_kwargs):
            return {"promoted": True, "graduated_via": "test-stub"}

        monkeypatch.setattr(server_module, "_db", object())
        monkeypatch.setattr(server_module, "_pipeline", object())
        monkeypatch.setattr(promotion_module, "promote_candidate", _stub_promote)

        sid = _sid("promote-accept")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="")

            result = await session_promote_candidate(
                sid, SessionPromoteCandidateRequest(candidate_id="cand-1", token=token)
            )
            assert result.get("promoted") is True


# ---------------------------------------------------------------------------
# Capability 13: cmd_current_phase reports next_gate + plan_hash
# ---------------------------------------------------------------------------


class TestCmdCurrentPhaseReportsBinding:
    """None of these mint a gate token (only a session cache), so none need
    _mint_cleanup or trip the autouse leak guard."""

    def test_reports_the_next_pending_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_current_phase
        from writ.session.cache import _read_cache, _write_cache

        sid = _sid("nextgate")
        _write_plan(tmp_path)
        cache = _read_cache(sid)
        cache.update({
            "mode": "work", "current_phase": "planning", "gates_approved": [],
            "project_root": str(tmp_path),
        })
        _write_cache(sid, cache)

        result = _call_json(cmd_current_phase, sid)
        assert result["next_gate"] == "phase-a"

    def test_reports_the_plan_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_current_phase
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.locators import plan_md_hash

        sid = _sid("planhash")
        _write_plan(tmp_path)
        expected = plan_md_hash(str(tmp_path))
        cache = _read_cache(sid)
        cache.update({
            "mode": "work", "current_phase": "planning", "gates_approved": [],
            "project_root": str(tmp_path),
        })
        _write_cache(sid, cache)

        result = _call_json(cmd_current_phase, sid)
        assert result["plan_hash"] == expected

    def test_next_gate_is_null_when_no_gate_is_pending(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_current_phase
        from writ.session.cache import _read_cache, _write_cache

        sid = _sid("nogatepending")
        cache = _read_cache(sid)
        cache.update({
            "mode": "work", "current_phase": "implementation",
            "gates_approved": ["phase-a", "test-skeletons"],
            "project_root": str(tmp_path),
        })
        _write_cache(sid, cache)

        result = _call_json(cmd_current_phase, sid)
        assert result["next_gate"] is None

    def test_plan_hash_is_null_when_there_is_no_plan_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.session.approval_workflow import cmd_current_phase
        from writ.session.cache import _read_cache, _write_cache

        sid = _sid("noplan")
        cache = _read_cache(sid)
        cache.update({
            "mode": "work", "current_phase": "planning", "gates_approved": [],
            "project_root": str(tmp_path),  # no plan.md written here
        })
        _write_cache(sid, cache)

        result = _call_json(cmd_current_phase, sid)
        assert result["plan_hash"] is None


# ---------------------------------------------------------------------------
# Capability 14: one friction event per refusal class
# ---------------------------------------------------------------------------


class TestFrictionEventPerRefusalClass:
    """Each of the three NEW refusal classes (gate mismatch, plan drift, unbound
    legacy token) must be distinguishable in the friction log from one another --
    otherwise a fail-closed gate reads identically to an absent one, which is the
    defect capability 14 exists to close."""

    def _advance_and_capture(self, tmp_path, monkeypatch, sid: str, token_body: str) -> tuple[dict, list[str]]:
        import writ.session.approval_workflow as aw
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import gate_token_path

        cache = _read_cache(sid)
        cache.update({"mode": "work", "current_phase": "planning", "gates_approved": []})
        _write_cache(sid, cache)

        with _mint_cleanup(sid):
            with open(gate_token_path(sid), "w") as f:
                f.write(token_body)

            events: list[str] = []
            monkeypatch.setattr(
                aw, "_log_friction_event",
                lambda session_id, mode, event, **extra: events.append(event),
            )
            result = _call_advance_json(
                monkeypatch, aw.cmd_advance_phase, sid, str(tmp_path), token_body.splitlines()[0]
            )
            return result, events

    def test_gate_mismatch_plan_drift_and_unbound_log_three_distinct_events(self, tmp_path, monkeypatch):
        from writ.session.locators import plan_md_hash

        _write_plan(tmp_path)
        current_hash = plan_md_hash(str(tmp_path))

        result_gate, events_gate = self._advance_and_capture(
            tmp_path, monkeypatch, _sid("friction-gate"),
            token_body=f"{uuid.uuid4().hex}\ntest-skeletons\n{current_hash}\n",
        )
        result_plan, events_plan = self._advance_and_capture(
            tmp_path, monkeypatch, _sid("friction-plan"),
            token_body=f"{uuid.uuid4().hex}\nphase-a\nstale-hash-value\n",
        )
        result_legacy, events_legacy = self._advance_and_capture(
            tmp_path, monkeypatch, _sid("friction-legacy"),
            token_body=f"{uuid.uuid4().hex}\n",
        )

        for label, result in (("gate", result_gate), ("plan", result_plan), ("legacy", result_legacy)):
            assert result["advanced"] is False, f"{label} mismatch must refuse the advance: {result}"

        def _only_refusal_event(events: list[str], label: str) -> str:
            candidates = [e for e in events if e != "phase_token_summary"]
            assert len(candidates) == 1, f"{label}: expected exactly one refusal friction event, got {events}"
            return candidates[0]

        e_gate = _only_refusal_event(events_gate, "gate-mismatch")
        e_plan = _only_refusal_event(events_plan, "plan-drift")
        e_legacy = _only_refusal_event(events_legacy, "unbound-legacy")

        assert len({e_gate, e_plan, e_legacy}) == 3, (
            "each refusal class must log a distinctly-named friction event; "
            f"got gate={e_gate!r} plan={e_plan!r} legacy={e_legacy!r}"
        )


# ---------------------------------------------------------------------------
# Capability 15: spend-on-rejection / no-spend-on-unresolved-root, unchanged
# ---------------------------------------------------------------------------


class TestSpendBehaviorUnchangedByTheBindingChange:
    """Both scenarios are already proven against the OLD claim_gate_token signature by
    tests/test_advance_phase_token_claim.py::TestPreservedBehaviorGuards; these re-run
    them through mint_gate_token so a regression introduced BY adding the gate=/
    plan_hash= binding is caught here too. RED today for an incidental reason only:
    mint_gate_token does not exist yet. The underlying spend/no-spend behavior itself
    is already correct and is not expected to change.
    """

    @pytest.mark.asyncio
    async def test_a_phase_a_rejection_still_spends_the_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.server import SessionAdvancePhaseRequest, session_advance_phase
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("rejectionspend")
        with _mint_cleanup(sid):
            cache = _read_cache(sid)
            cache.update({"mode": "work", "current_phase": "planning", "gates_approved": []})
            _write_cache(sid, cache)
            empty_project = tmp_path / "empty_project"
            empty_project.mkdir()  # deliberately no plan.md: phase-a validation must fail

            token = mint_gate_token(sid, gate="phase-a", plan_hash="")
            result = await session_advance_phase(
                sid,
                SessionAdvancePhaseRequest(
                    confirmation_source="explicit", token=token, project_root=str(empty_project),
                ),
            )
            assert result.get("advanced") is False
            assert result.get("gate") == "phase-a"
            assert not os.path.exists(gate_token_path(sid)), (
                "a phase-a validation rejection must still consume the spent token"
            )

    @pytest.mark.asyncio
    async def test_an_unresolvable_project_root_does_not_spend_the_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        from writ.server import SessionAdvancePhaseRequest, session_advance_phase
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("noroot")
        with _mint_cleanup(sid):
            cache = _read_cache(sid)
            cache.update({"mode": "work", "current_phase": "planning", "gates_approved": []})
            _write_cache(sid, cache)

            token = mint_gate_token(sid, gate="phase-a", plan_hash="")
            # No project_root, no cwd -> resolve_project_root returns ("", "none").
            result = await session_advance_phase(
                sid, SessionAdvancePhaseRequest(confirmation_source="explicit", token=token),
            )
            assert result.get("advanced") is False
            assert result.get("token_spent") is False
            assert os.path.exists(gate_token_path(sid)), (
                "an unresolvable project root must NOT spend the token -- the user can "
                "retry from the project directory without re-approving"
            )
