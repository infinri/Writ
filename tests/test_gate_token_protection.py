"""The approval credential: unforgeable, bound to what it authorizes, spent once.

`HANDBOOK.md` calls the gate token the control that keeps the human the approver of canon.
Four of its documented or implied properties were not true:

  * THE FILE WAS NOT PROTECTED. `grep -rn "writ-gate-token" hooks/` returned exactly one
    hit, the mint in auto-approve-gate.sh. The Bash state-deny case
    (writ-bash-write-gate.sh) and the Write/Edit guard (writ-state-write-gate.sh) both
    listed the session-cache dir, the pointer file, `writ-session-`, `manual_test_grant`
    and `writ-grant-`, and omitted the credential. Blanking line 2 converted a genuine
    phase approval into the empty-gate credential the promotion route accepts, while
    line 1 stayed the authentic secret: a three-byte edit, not a forgery.
  * IT BOUND NO CANDIDATE. The promotion route took candidate_id from the request body, so
    one approval authorized the promotion of any candidate the caller named. A fourth
    token line now carries the candidate the agent surfaced BEFORE the human approved,
    exactly as line 3 carries the plan fingerprint.
  * IT WAS NOT CLAIMED ATOMICALLY. The route read the token, then consumed it only after
    promote_candidate returned, so two concurrent posts both wrote canon. The advance
    route never had this problem because it uses claim_gate_token's claim-by-rename.
  * THE REVIEW ARTIFACT HAD NO CALLER. build_promotion_review_artifact exists to make the
    human "the APPROVER of canon, not a veto switch reacting to an id" (its own words) and
    nothing in production called it. The new review route serves it AND records the
    candidate, which is what gives the mint something to bind to.

WHAT THIS MODULE DELIBERATELY DOES NOT TEST, so nobody hunts for a missing test:

  * The two corrected HANDBOOK.md sentences. Asserting on documentation prose is forbidden
    here (doc staleness is not a code defect); they are verified by review.
  * The operational capability, which needs a real session to hand-run a refused Write.

Harness mirrors the modules it borrows from rather than importing across test files, which
is this suite's convention: `_env`/`_run_hook` from tests/test_gate_decision_mode_stamp.py,
and `_sid`/`_mint_cleanup` plus the CHILD-PROCESS concurrency shape from
tests/test_gate_token_binding.py. Real processes are required for the atomicity proof, not
threads: os.rename's guarantee is cross-process, and threads in one interpreter share a GIL.
"""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
HELPER = REPO / "bin" / "lib" / "writ-session.py"

CANDIDATE = "TEST-CAND-001"
OTHER_CANDIDATE = "TEST-CAND-002"


def _sid(label: str) -> str:
    return f"gtp-{label}-{uuid.uuid4().hex[:8]}"


def _env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
    env["WRIT_FRICTION_LOG"] = str(tmp_path / "friction.log")
    env["WRIT_NO_AUTOSTART"] = "1"
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    return env


def _run_hook(tmp_path: Path, hook: str, envelope: dict) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["bash", str(HOOKS / hook)], input=json.dumps(envelope),
        capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )
    assert res.returncode == 0, f"{hook} exited {res.returncode}: {res.stderr}"
    return res


def _decision(res: subprocess.CompletedProcess) -> str:
    """The hook's verdict, or "" when it emitted none (which means allow-by-silence)."""
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        for key in ("permissionDecision", "decision"):
            if key in payload:
                return str(payload[key])
        hook_out = payload.get("hookSpecificOutput") or {}
        if "permissionDecision" in hook_out:
            return str(hook_out["permissionDecision"])
    return ""


@contextlib.contextmanager
def _mint_cleanup(sid: str):
    from writ.session.gate_token import gate_token_path
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(gate_token_path(sid))


def _review_request(candidate_id: str):
    """The new route's request body. ImportError until the model lands, scoped to
    the calling test because this is imported here rather than at module scope."""
    from writ.server.models import SessionPromotionReviewRequest
    return SessionPromotionReviewRequest(candidate_id=candidate_id)


def _seed_pending_candidate(tmp_path: Path, sid: str, candidate_id: str) -> None:
    """Record a surfaced candidate the way the review route will, via the locked writer."""
    env_dir = _env(tmp_path)["WRIT_CACHE_DIR"]
    subprocess.run(
        [sys.executable, "-c",
         "import os,sys;os.environ['WRIT_CACHE_DIR']=sys.argv[1];"
         "from writ.session.cache import mutate_cache;"
         "c=mutate_cache(sys.argv[2]);"
         "ctx=c.__enter__();ctx['pending_candidate_id']=sys.argv[3];c.__exit__(None,None,None)",
         env_dir, sid, candidate_id],
        check=True, capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


def _current_phase(tmp_path: Path, sid: str) -> dict:
    """The reply the mint reads its binding values out of."""
    res = subprocess.run(
        [sys.executable, str(HELPER), "current-phase", sid],
        capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )
    out = (res.stdout or "").strip()
    return json.loads(out) if out.startswith("{") else {}


async def _promote_returning_sid(
    monkeypatch, *, minted_candidate, requested_candidate,
    gate: str = "", plan_hash: str = "", unbound: bool = False,
    promote_succeeds: bool = True,
):
    """Mint a token with a chosen binding, then drive the real promotion route.

    promote_candidate itself is stubbed: this module is about the CREDENTIAL, not about
    graph writes, and the real one needs a live graph. Everything between the request and
    that stub is production code.
    """
    import writ.promotion as promotion_module
    import writ.server as server_module
    from writ.server.models import SessionPromoteCandidateRequest
    from writ.server.routes.gate import session_promote_candidate
    from writ.session.gate_token import gate_token_path, mint_gate_token

    async def _stub_promote(*_args, **_kwargs):
        if promote_succeeds:
            return {"promoted": True, "graduated_via": "test-stub"}
        return {"promoted": False, "error": "structural gate rejected the edited text"}

    monkeypatch.setattr(server_module, "_db", object())
    monkeypatch.setattr(server_module, "_pipeline", object())
    monkeypatch.setattr(promotion_module, "promote_candidate", _stub_promote)

    sid = _sid("promote")
    with _mint_cleanup(sid):
        if minted_candidate is None:
            token = mint_gate_token(sid, gate=gate, plan_hash=plan_hash)
        else:
            token = mint_gate_token(sid, gate=gate, plan_hash=plan_hash,
                                    candidate_id=minted_candidate)
        if unbound:
            # The pre-binding format: one line, no gate and no fingerprint.
            Path(gate_token_path(sid)).write_text(f"{token}\n")
        result = await session_promote_candidate(
            sid, SessionPromoteCandidateRequest(
                candidate_id=requested_candidate, token=token),
        )
        return sid, result


async def _promote(monkeypatch, **kwargs) -> dict:
    _sid_used, result = await _promote_returning_sid(monkeypatch, **kwargs)
    return result


def _promo_claim_worker(sid: str, token: str, candidate_id: str, barrier, queue) -> None:
    """Runs in a CHILD PROCESS: os.rename's atomicity is a cross-process guarantee."""
    from writ.session.gate_token import claim_gate_token

    barrier.wait()
    queue.put(claim_gate_token(sid, token, gate="", plan_hash="",
                               candidate_id=candidate_id))


class TestTokenPathProtectedFromWriteEdit:
    def test_write_to_the_token_path_is_refused(self, tmp_path):
        sid = _sid("we-deny")
        res = _run_hook(tmp_path, "writ-state-write-gate.sh", {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": f"/tmp/writ-gate-token-{sid}", "content": "x"},
        })
        assert _decision(res) == "deny"

    def test_edit_to_the_token_path_is_refused(self, tmp_path):
        sid = _sid("we-edit")
        res = _run_hook(tmp_path, "writ-state-write-gate.sh", {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": f"/tmp/writ-gate-token-{sid}", "content": "x"},
        })
        assert _decision(res) == "deny"

    def test_an_unrelated_tmp_path_is_untouched(self, tmp_path):
        """The new entry must name the credential, not blanket-deny /tmp."""
        sid = _sid("we-allow")
        res = _run_hook(tmp_path, "writ-state-write-gate.sh", {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/some-unrelated-scratch.txt", "content": "x"},
        })
        assert _decision(res) != "deny"


class TestTokenPathProtectedFromBash:
    def test_bash_write_to_the_token_path_is_refused(self, tmp_path):
        sid = _sid("bash-deny")
        res = _run_hook(tmp_path, "writ-bash-write-gate.sh", {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"printf 'x\\n' > /tmp/writ-gate-token-{sid}"},
        })
        assert _decision(res) == "deny"

    def test_read_only_inspection_is_still_allowed(self, tmp_path):
        """Matches the existing carve-out for the other gate-state paths."""
        sid = _sid("bash-read")
        res = _run_hook(tmp_path, "writ-bash-write-gate.sh", {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"cat /tmp/writ-gate-token-{sid}"},
        })
        assert _decision(res) != "deny"


def _stub_review_daemon(monkeypatch, *, raises: str = ""):
    """Present the daemon collaborators and stub the artifact builder.

    The builder itself needs a live graph and an embedding model; what is under test here
    is that the route returns what the builder produced AND records what it surfaced.
    """
    import writ.server as server_module

    async def _stub_artifact(candidate_id, _pipeline, _db):
        if raises:
            raise ValueError(raises)
        return {"statement": "s", "trigger": "t", "nearest": [], "conflicts": []}

    monkeypatch.setattr(server_module, "_db", object())
    monkeypatch.setattr(server_module, "_pipeline", object())
    monkeypatch.setattr(server_module, "build_promotion_review_artifact", _stub_artifact)


class TestPromotionReviewRoute:
    @pytest.mark.asyncio
    async def test_review_returns_the_artifact_and_records_the_candidate(self, monkeypatch):
        """One call must do both: show the human real content, and record what was shown."""
        from writ.server.routes.gate import session_promotion_review
        from writ.session.cache import _read_cache

        _stub_review_daemon(monkeypatch)
        sid = _sid("review-ok")
        result = await session_promotion_review(sid, _review_request(CANDIDATE))
        assert result.get("candidate_id") == CANDIDATE
        assert result.get("nearest") is not None, "the review artifact must be returned"
        assert _read_cache(sid).get("pending_candidate_id") == CANDIDATE

    @pytest.mark.asyncio
    async def test_a_non_pending_candidate_is_refused_and_not_recorded(self, monkeypatch):
        """A rejected surfacing must not leave a binding for a later approval to pick up."""
        from writ.server.routes.gate import session_promotion_review
        from writ.session.cache import _read_cache

        _stub_review_daemon(monkeypatch, raises="not a graduation_pending candidate")
        sid = _sid("review-bad")
        result = await session_promotion_review(sid, _review_request("NOT-PENDING-001"))
        assert result.get("error")
        assert not _read_cache(sid).get("pending_candidate_id")


class TestCurrentPhaseReportsTheCandidate:
    def test_recorded_candidate_is_reported(self, tmp_path):
        sid = _sid("phase-cand")
        _seed_pending_candidate(tmp_path, sid, CANDIDATE)
        assert _current_phase(tmp_path, sid).get("candidate_id") == CANDIDATE

    def test_absent_candidate_reports_empty(self, tmp_path):
        sid = _sid("phase-none")
        assert not _current_phase(tmp_path, sid).get("candidate_id")


class TestTokenCarriesTheCandidate:
    def test_mint_writes_the_candidate_on_the_fourth_line(self):
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("mint-cand")
        with _mint_cleanup(sid):
            mint_gate_token(sid, gate="", plan_hash="", candidate_id=CANDIDATE)
            lines = Path(gate_token_path(sid)).read_text().split("\n")
            assert lines[3] == CANDIDATE

    def test_a_three_line_token_reads_as_no_candidate(self):
        """Bounds-safe: a file with no trailing newline yields exactly three elements."""
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("legacy")
        with _mint_cleanup(sid):
            mint_gate_token(sid, gate="", plan_hash="")
            Path(gate_token_path(sid)).write_text("secret\n\n")
            from writ.session.gate_token import read_gate_candidate
            assert read_gate_candidate(sid) == ""


class TestPromotionCandidateBinding:
    @pytest.mark.asyncio
    async def test_a_different_candidate_is_refused(self, monkeypatch):
        """The confused-deputy case: approval for C must not promote D."""
        result = await _promote(monkeypatch, minted_candidate=CANDIDATE,
                                requested_candidate=OTHER_CANDIDATE)
        assert result.get("promoted") is False
        error = result.get("error") or ""
        assert CANDIDATE in error and OTHER_CANDIDATE in error, (
            "the refusal must name what the approval DID authorize and what was asked "
            f"for, so the operator can tell which is wrong: {error!r}"
        )

    @pytest.mark.asyncio
    async def test_the_bound_candidate_promotes(self, monkeypatch):
        result = await _promote(monkeypatch, minted_candidate=CANDIDATE,
                                requested_candidate=CANDIDATE)
        assert result.get("promoted") is True

    @pytest.mark.asyncio
    async def test_a_legacy_three_line_token_cannot_promote(self, monkeypatch):
        """Fails in the safe direction: a credential minted before the binding existed."""
        result = await _promote(monkeypatch, minted_candidate=None,
                                requested_candidate=CANDIDATE)
        assert result.get("promoted") is False

    def test_a_legacy_three_line_token_still_advances_a_phase(self, tmp_path):
        """Every token already on disk must keep working for what it WAS bound to."""
        from writ.session.gate_token import claim_gate_token, mint_gate_token

        sid = _sid("legacy-advance")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123")
            assert claim_gate_token(sid, token, gate="phase-a", plan_hash="abc123") is True


class TestPromotionIsClaimedAtomically:
    def test_two_real_processes_claiming_one_token_yield_one_win(self):
        """os.rename's atomicity is a CROSS-PROCESS guarantee, so this uses processes."""
        from writ.session.gate_token import mint_gate_token

        sid = _sid("atomic")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="", candidate_id=CANDIDATE)
            barrier = multiprocessing.Barrier(2)
            queue: multiprocessing.Queue = multiprocessing.Queue()
            procs = [
                multiprocessing.Process(target=_promo_claim_worker,
                                        args=(sid, token, CANDIDATE, barrier, queue))
                for _ in range(2)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
            results = [queue.get() for _ in range(2)]
            assert sorted(results) == [False, True], results


class TestPromotionBehavioursThatMustNotChange:
    @pytest.mark.asyncio
    async def test_a_phase_bound_token_is_still_refused(self, monkeypatch):
        result = await _promote(monkeypatch, minted_candidate=CANDIDATE,
                                requested_candidate=CANDIDATE, gate="phase-a")
        assert result.get("promoted") is False

    @pytest.mark.asyncio
    async def test_an_unbound_token_is_still_refused(self, monkeypatch):
        result = await _promote(monkeypatch, minted_candidate=None,
                                requested_candidate=CANDIDATE, unbound=True)
        assert result.get("promoted") is False

    @pytest.mark.asyncio
    async def test_plan_drift_does_not_refuse(self, monkeypatch):
        """The documented asymmetry: a promotion's artifact is the candidate, not plan.md."""
        result = await _promote(monkeypatch, minted_candidate=CANDIDATE,
                                requested_candidate=CANDIDATE, plan_hash="stale-fp")
        assert result.get("promoted") is True

    @pytest.mark.asyncio
    async def test_a_failed_promotion_still_spends_the_approval(self, monkeypatch):
        """CORRECTS AN APPROVED CAPABILITY. The plan said a failed promotion leaves the
        token unspent so a retry is possible. That is incompatible with the atomic claim
        the same plan asked for: claiming is the mutual exclusion, and it necessarily
        happens BEFORE the work it protects. /advance-phase already resolved this the same
        way, spending the approval on a judged-and-failed artifact with the reasoning that
        a changed artifact needs a fresh approval. A rejected promotion is the same case:
        the approval was for the text that was rejected."""
        from writ.session.gate_token import read_gate_token

        sid, result = await _promote_returning_sid(
            monkeypatch, minted_candidate=CANDIDATE, requested_candidate=CANDIDATE,
            promote_succeeds=False)
        assert result.get("promoted") is False
        assert not read_gate_token(sid), (
            "the claim is the mutual exclusion, so it is spent whether or not the "
            "promotion it protected succeeded"
        )
