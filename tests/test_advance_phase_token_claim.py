"""tests/test_advance_phase_token_claim.py

Wave 1 Cycle 5 -- Decision G1: advance-phase concurrent same-token double-fire.

Three parts (plan.md "Decision G1"):

  TestClaimGateTokenPrimitive     -- the atomic claim primitive, `_claim_token_mutex`
                                      (renamed from claim_gate_token once that name
                                      became the gate=/plan_hash=-bound public function
                                      -- see the note below the three parts). RED today
                                      for a mechanical reason, not a new-capability one:
                                      _claim_token_mutex does not exist yet under that
                                      name, so every test in this class raises
                                      ImportError until the rename lands. It pins the
                                      SAME behavior claim_gate_token has always had; no
                                      assertion here changed. Each test imports the name
                                      LOCALLY (not at module scope) so that ImportError
                                      is scoped to this class and does not block
                                      collection/execution of the other two classes
                                      below.

  TestServerConcurrentDoubleFire  -- the CENTERPIECE. Drives two concurrent
                                      session_advance_phase() calls with the SAME
                                      valid token via asyncio.gather and asserts
                                      exactly one real advance + side effects
                                      firing exactly once. RED today for the real
                                      reason: session_advance_phase has no claim/
                                      mutual-exclusion mechanism, so BOTH calls
                                      advance (phase_transitions grows to 2, the
                                      phase_advance friction event fires twice).

  TestPreservedBehaviorGuards     -- single-call behavior the G1 restructure must
                                      not regress. These are single-invocation
                                      scenarios, UNAFFECTED by the concurrency bug,
                                      so they already PASS against today's
                                      (unfixed) code. They exist to catch a
                                      regression introduced BY the fix (e.g. the
                                      claim landing at the wrong point in the
                                      ordering, or a no-op accidentally consuming
                                      the token). Intent is documented per test.

Cycle 1 (plan.md finding 4) touches this file too, in the opposite direction from an
earlier draft: the bare rename-mutex this class exercises is kept as its own internal
primitive, `_claim_token_mutex(session_id, supplied_token)`, UNCHANGED in behavior --
review rejected giving the public `claim_gate_token` fail-open `gate=None`/
`plan_hash=None` defaults ("do not enforce this half of the binding"), because a
default on the function that decides whether a human approved an action means a caller
that forgets the argument gets an unguarded claim and no error. `claim_gate_token`
(tests/test_gate_token_binding.py owns its contract) now REQUIRES gate= and plan_hash=
keyword-only, with no default, so omitting either raises TypeError instead of silently
skipping enforcement. The four call sites below therefore go back to their ORIGINAL
two-positional-argument call -- `_claim_token_mutex(sid, token)`, no gate=/plan_hash= at
all -- and are UNCHANGED by, and not part of, this cycle's red set.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest

from tests.fixtures.session_state import write_bound_gate_token
from writ.server import SessionAdvancePhaseRequest
from writ.session.cache import _read_cache, _write_cache
from writ.session.gate_token import gate_token_path


def _seed_cache(session_id: str, **overrides) -> dict:
    """Minimal work-mode cache seed at the 'test-skeletons pending' gate; only
    the fields a given test actually needs are overridden per call."""
    cache = _read_cache(session_id)
    cache.update({
        "mode": "work",
        "current_phase": "testing",
        "gates_approved": ["phase-a"],
        "denial_counts": {},
        "loaded_rule_ids_by_phase": {},
        "phase_transitions": [],
    })
    cache.update(overrides)
    _write_cache(session_id, cache)
    return cache


def _write_token(session_id: str, token: str) -> str:
    """Write the PRE-BINDING one-line token file: the bare mutex's input.

    _claim_token_mutex compares the WHOLE file against the supplied token, so its four
    tests below need exactly this shape and must keep it. The two route tests use
    _write_bound_token instead: the advance route claims through claim_gate_token with no
    unbound fallback, so a one-line file is refused there before any gate logic runs.
    """
    path = gate_token_path(session_id)
    with open(path, "w") as f:
        f.write(token)
    return path


def _write_bound_token(session_id: str, token: str) -> str:
    """Write the three-line BOUND token file a genuine approval mints; return its path.

    The binding (gate + plan fingerprint) is derived from the already-seeded cache, which
    is what the production mint does, so the route's claim recomputes the same two values.
    """
    write_bound_gate_token(session_id, token)
    return gate_token_path(session_id)


def _project_with_test_skeleton(tmp_path) -> str:
    """A project root whose test-skeletons gate validates, returned as a str path.

    These tests seed the 'test-skeletons pending' gate and care only about the token
    CLAIM. The route now runs the target gate's validator (it used to validate phase-a
    only, so test-skeletons advanced with no artifact check at all), which means a
    request with no resolvable root is refused before the claim is reached. Supplying a
    root that genuinely passes _validate_test_skeletons keeps these tests aimed at
    concurrency instead of accidentally re-testing validation.
    """
    tests_dir = tmp_path / "proj" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_skeleton.py").write_text("def test_placeholder():\n    assert True\n")
    return str(tmp_path / "proj")


# ---------------------------------------------------------------------------
# G1(a) -- the claim primitive (concurrency, absent, mismatched)
# ---------------------------------------------------------------------------


class TestClaimGateTokenPrimitive:
    """_claim_token_mutex(session_id, supplied_token) -> bool.

    The token FILE is the mutual-exclusion primitive (os.rename-as-mutex):
    exactly one of N concurrent callers renaming the same source file wins.

    This is the SAME function today's claim_gate_token is, just renamed and kept as an
    internal primitive once the public claim_gate_token gains required gate=/plan_hash=
    binding enforcement (tests/test_gate_token_binding.py). These four tests are
    UNCHANGED by that cycle -- they import _claim_token_mutex, not claim_gate_token, and
    call it with the same two positional arguments as before.
    """

    def test_exactly_one_of_n_concurrent_claims_wins(self):
        from writ.session.gate_token import _claim_token_mutex

        sid = f"g1a-race-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        _write_token(sid, token)

        n = 8
        start = threading.Barrier(n, timeout=5)
        results: list[bool] = []
        results_lock = threading.Lock()

        def _claim() -> None:
            start.wait()
            outcome = _claim_token_mutex(sid, token)
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=_claim) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == n, f"expected {n} results, got {len(results)}: {results}"
        assert results.count(True) == 1, (
            f"exactly one of {n} concurrent claims for the SAME token must win; got {results}"
        )
        assert results.count(False) == n - 1

    def test_winner_removes_the_token_file(self):
        from writ.session.gate_token import _claim_token_mutex

        sid = f"g1a-remove-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        path = _write_token(sid, token)

        assert _claim_token_mutex(sid, token) is True
        assert not os.path.exists(path), "a winning claim must remove the token file"

    def test_claim_on_absent_token_returns_false(self):
        from writ.session.gate_token import _claim_token_mutex

        sid = f"g1a-absent-{uuid.uuid4().hex[:8]}"
        # No token file was ever written for this fresh session id.
        assert _claim_token_mutex(sid, "any-token") is False

    def test_claim_with_mismatched_token_returns_false_and_removes_file(self):
        from writ.session.gate_token import _claim_token_mutex

        sid = f"g1a-mismatch-{uuid.uuid4().hex[:8]}"
        path = _write_token(sid, "the-real-token")
        try:
            claimed = _claim_token_mutex(sid, "a-wrong-token")
            assert claimed is False
            assert not os.path.exists(path), (
                "a mismatched claim must still remove the (wrongly-claimed) token "
                "file -- fail-closed, mirrors gate_token_valid's fail-closed contract"
            )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# G1(b) -- server concurrent double-fire (CENTERPIECE)
# ---------------------------------------------------------------------------


class TestServerConcurrentDoubleFire:
    """Two concurrent /advance-phase calls carrying the SAME valid token must
    advance exactly once; the loser must be a side-effect-free no-op.

    Seeded at a NON-planning phase (current_phase='testing', gates_approved=
    ['phase-a'], mode='work') so neither the plan.md phase-a validator nor
    Neo4j (_db is None outside the app lifespan) is required -- isolates the
    test to the concurrency defect itself.

    The token-read and cache-read call sites are wrapped with
    threading.Barrier(2) stubs that delegate to the REAL functions. This is
    NOT mocking away the bug: it exists only to remove read-side thread-
    scheduling nondeterminism. An unsynchronized asyncio.gather could, on an
    unlucky scheduling, let call A fully complete (consume the token, advance
    the cache) before call B even performs its first read -- call B would then
    correctly see a consumed token / an already-advanced cache and no-op, a
    FALSE NEGATIVE for this test (it would look fixed when it is not). The
    barriers force both calls to observe the token as valid and the cache as
    'test-skeletons pending' before either proceeds, which is the actual
    concurrent-approval scenario this test exists to reproduce. Everything
    downstream of that point -- the real mutate_cache flock, the real
    consume_gate_token, the real friction-log call count -- runs unmocked,
    because THAT is the code path under test.
    """

    @pytest.fixture(autouse=True)
    def _cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

    @pytest.mark.asyncio
    async def test_two_concurrent_advances_same_token_advance_exactly_once(self, monkeypatch, tmp_path):
        import writ.server as server_module
        from writ.session.cache import _read_cache as real_read_cache
        from writ.session.gate_token import read_gate_token as real_read_gate_token

        sid = f"g1b-{uuid.uuid4().hex[:8]}"
        _seed_cache(sid)
        token = uuid.uuid4().hex
        token_path = _write_bound_token(sid, token)

        token_barrier = threading.Barrier(2, timeout=5)
        cache_barrier = threading.Barrier(2, timeout=5)

        def _synced_read_token(session_id: str) -> str:
            token_barrier.wait()
            return real_read_gate_token(session_id)

        def _synced_read_cache(session_id: str) -> dict:
            cache_barrier.wait()
            return real_read_cache(session_id)

        monkeypatch.setattr(server_module, "read_gate_token", _synced_read_token)
        monkeypatch.setattr(server_module.writ_session, "_read_cache", _synced_read_cache)

        friction_calls: list[dict] = []
        monkeypatch.setattr(
            server_module,
            "log_friction_event",
            lambda *a, **k: friction_calls.append(k),
        )

        body = {
            "confirmation_source": "explicit",
            "token": token,
            "project_root": _project_with_test_skeleton(tmp_path),
        }
        try:
            r1, r2 = await asyncio.gather(
                server_module.session_advance_phase(sid, SessionAdvancePhaseRequest(**body)),
                server_module.session_advance_phase(sid, SessionAdvancePhaseRequest(**body)),
            )
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass

        def _is_real_advance(r: dict) -> bool:
            return "error" not in r and r.get("advanced") is not False and "phase" in r

        real_advances = [r for r in (r1, r2) if _is_real_advance(r)]
        noops = [r for r in (r1, r2) if r.get("advanced") is False]

        assert len(real_advances) == 1, (
            f"exactly one concurrent request must actually advance; got r1={r1!r} r2={r2!r}"
        )
        assert len(noops) == 1, (
            "the losing concurrent request must return a side-effect-free "
            f"advanced: False no-op; got r1={r1!r} r2={r2!r}"
        )

        final_cache = real_read_cache(sid)
        assert len(final_cache.get("phase_transitions", [])) == 1, (
            "one token/approval must produce exactly one phase_transitions "
            f"record (double-fire): got {final_cache.get('phase_transitions')}"
        )

        phase_advance_events = [k for k in friction_calls if k.get("event") == "phase_advance"]
        assert len(phase_advance_events) == 1, (
            "the phase_advance friction event must fire exactly once per "
            f"token; fired {len(phase_advance_events)} times"
        )


# ---------------------------------------------------------------------------
# G1(c) -- preserved-behavior guards
# ---------------------------------------------------------------------------


class TestPreservedBehaviorGuards:
    """Single-call scenarios the G1 restructure must not regress.

    None of these exercise concurrency, so all three already pass against
    today's unfixed code -- they pin behavior the claim-based restructure must
    keep true, not the bug being fixed.
    """

    @pytest.fixture(autouse=True)
    def _cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

    @pytest.mark.asyncio
    async def test_single_valid_token_advance_still_advances(self, tmp_path):
        """Already true today: a lone call with a valid token performs a real
        advance. Must remain true once the claim replaces the post-advance
        consume_gate_token."""
        from writ.server import session_advance_phase

        sid = f"g1c-single-{uuid.uuid4().hex[:8]}"
        _seed_cache(sid)  # current_phase=testing, gates_approved=[phase-a]
        token = uuid.uuid4().hex
        token_path = _write_bound_token(sid, token)
        try:
            result = await session_advance_phase(
                sid,
                SessionAdvancePhaseRequest(
                    confirmation_source="explicit",
                    token=token,
                    project_root=_project_with_test_skeleton(tmp_path),
                ),
            )
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass

        assert "error" not in result, result
        assert result.get("phase") == "implementation", result

    @pytest.mark.asyncio
    async def test_noop_advance_does_not_consume_token(self):
        """Already true today: a no-op (all gates already approved) must not
        consume the token -- the file must remain so a subsequent genuine
        advance can still use it."""
        from writ.server import session_advance_phase

        sid = f"g1c-noop-{uuid.uuid4().hex[:8]}"
        _seed_cache(
            sid,
            current_phase="implementation",
            gates_approved=["phase-a", "test-skeletons"],
        )
        token = uuid.uuid4().hex
        token_path = _write_token(sid, token)
        try:
            result = await session_advance_phase(
                sid, SessionAdvancePhaseRequest(confirmation_source="explicit", token=token)
            )
            assert result.get("advanced") is False, result
            assert os.path.exists(token_path), (
                "a no-op advance must NOT consume the gate token -- the token "
                "file must still exist afterward"
            )
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass

    @pytest.mark.asyncio
    async def test_phase_a_rejection_consumes_token(self, tmp_path):
        """Already true today: a phase-a validation rejection (no plan.md
        found) consumes the spent token. Unchanged by G1 (the plan explicitly
        keeps consume_gate_token, not the claim, on this rejection path)."""
        from writ.server import session_advance_phase

        sid = f"g1c-reject-{uuid.uuid4().hex[:8]}"
        _seed_cache(sid, current_phase="planning", gates_approved=[])
        token = uuid.uuid4().hex
        token_path = _write_token(sid, token)
        project_dir = tmp_path / "empty_project"
        project_dir.mkdir()
        try:
            result = await session_advance_phase(
                sid,
                SessionAdvancePhaseRequest(
                    confirmation_source="explicit",
                    token=token,
                    project_root=str(project_dir),
                ),
            )
            assert result.get("advanced") is False, result
            assert result.get("gate") == "phase-a", result
            assert not os.path.exists(token_path), (
                "a phase-a validation rejection must consume the spent token "
                "(no reuse -- a changed plan needs a fresh approval)"
            )
        finally:
            try:
                os.remove(token_path)
            except OSError:
                pass
