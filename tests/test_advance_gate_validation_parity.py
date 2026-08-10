"""The advance route must validate the TARGET gate, and say what it validated.

Three defects in the live advance path (`POST /session/{id}/advance-phase`), which is
the one both the approval hook and /writ-approve use:

  1. It validated phase-a only, inline. `_GATE_VALIDATORS` registers a validator for
     `test-skeletons` too, but only the CLI dispatched the registry, so on the live path
     the test-skeletons gate advanced without checking that any test skeleton exists.
  2. An unresolvable project root SPENT the approval token, even though no artifact was
     ever judged. The human could not fix it by editing anything, and the next
     "approved" burned another token.
  3. The response said nothing about which project root or plan.md was accepted, so a
     root resolved from a stray marker file above the work directory silently stamped an
     unrelated plan.

These drive session_advance_phase() in-process (no daemon) with WRIT_CACHE_DIR pointed
at tmp_path.
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.fixtures.session_state import write_bound_gate_token
from writ.server import SessionAdvancePhaseRequest
from writ.session.cache import _read_cache, _write_cache
from writ.session.gate_token import gate_token_path

PLAN_OK = """# Plan

## Files
- `src/foo.py` (create) -- the thing

## Analysis
Because.

## Rules Applied
No matching rules

## Capabilities
- [ ] it works
"""


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()


def _seed(session_id: str, **overrides) -> None:
    cache = _read_cache(session_id)
    cache.update({
        "mode": "work",
        "current_phase": "planning",
        "gates_approved": [],
        "denial_counts": {},
        "phase_transitions": [],
    })
    cache.update(overrides)
    _write_cache(session_id, cache)


def _token(session_id: str) -> str:
    # A BOUND token (gate + plan fingerprint), derived from the seeded cache the way the
    # production mint derives it. The route claims through claim_gate_token with no
    # unbound fallback, so a bare one-line secret is refused before any gate logic runs.
    return write_bound_gate_token(session_id, uuid.uuid4().hex)


def _token_exists(session_id: str) -> bool:
    return os.path.exists(gate_token_path(session_id))


def _planning_project(tmp_path, plan_body: str = PLAN_OK) -> str:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "plan.md").write_text(plan_body)
    return str(root)


def _testing_project(tmp_path, with_skeleton: bool = True) -> str:
    root = tmp_path / "proj2"
    (root / "tests").mkdir(parents=True, exist_ok=True)
    if with_skeleton:
        (root / "tests" / "test_thing.py").write_text("def test_thing():\n    assert True\n")
    return str(root)


async def _advance(session_id: str, **body):
    from writ.server import session_advance_phase

    return await session_advance_phase(session_id, SessionAdvancePhaseRequest(**body))


# --------------------------------------------------------------------------- #
# 1. the test-skeletons gate is enforced on the live path
# --------------------------------------------------------------------------- #
class TestTestSkeletonsGateEnforced:
    @pytest.mark.asyncio
    async def test_missing_skeletons_rejects(self, tmp_path):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid, current_phase="testing", gates_approved=["phase-a"])
        tok = _token(sid)
        res = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_testing_project(tmp_path, with_skeleton=False),
        )
        assert res.get("advanced") is False
        assert res.get("gate") == "test-skeletons"
        assert "test files" in (res.get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_present_skeletons_advance(self, tmp_path):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid, current_phase="testing", gates_approved=["phase-a"])
        tok = _token(sid)
        res = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_testing_project(tmp_path, with_skeleton=True),
        )
        assert res.get("phase") == "implementation", res

    @pytest.mark.asyncio
    async def test_rejection_spends_the_token(self, tmp_path):
        """A judged-and-failed artifact needs a FRESH approval (governance rule)."""
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid, current_phase="testing", gates_approved=["phase-a"])
        tok = _token(sid)
        res = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_testing_project(tmp_path, with_skeleton=False),
        )
        assert res.get("token_spent") is True
        assert not _token_exists(sid), "a rejected artifact must consume the approval"


# --------------------------------------------------------------------------- #
# 2. phase-a keeps working, from a marker-less cwd too
# --------------------------------------------------------------------------- #
class TestPhaseAUnchangedPlusCwd:
    @pytest.mark.asyncio
    async def test_valid_plan_advances_via_explicit_root(self, tmp_path):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        res = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_planning_project(tmp_path),
        )
        assert res.get("phase") == "testing", res

    @pytest.mark.asyncio
    async def test_malformed_plan_rejects_and_spends(self, tmp_path):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        res = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_planning_project(tmp_path, plan_body="# Plan\n\nno sections\n"),
        )
        assert res.get("advanced") is False
        assert res.get("gate") == "phase-a"
        assert not _token_exists(sid)

    @pytest.mark.asyncio
    async def test_unmarked_cwd_can_advance(self, tmp_path):
        """The headline fix: no repo marker anywhere, approval still works."""
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        work = _planning_project(tmp_path)
        assert not any(
            os.path.exists(os.path.join(work, m))
            for m in ("composer.json", "package.json", "Cargo.toml", "go.mod", "pyproject.toml", ".git")
        )
        res = await _advance(sid, confirmation_source="explicit", token=tok, cwd=work)
        assert res.get("phase") == "testing", res
        assert res.get("root_tier") == "cwd"
        assert res.get("project_root") == work


# --------------------------------------------------------------------------- #
# 3. unresolvable root fails closed WITHOUT spending the approval
# --------------------------------------------------------------------------- #
class TestUnresolvableRootDoesNotSpend:
    @pytest.mark.asyncio
    async def test_no_root_refuses_advance(self):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        res = await _advance(sid, confirmation_source="explicit", token=tok)
        assert res.get("advanced") is False
        assert res.get("gate") == "phase-a"
        assert res.get("root_tier") == "none"

    @pytest.mark.asyncio
    async def test_no_root_keeps_the_token(self):
        """An infra failure must not burn the human's approval."""
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        _token(sid)
        res = await _advance(sid, confirmation_source="explicit", token=_read_token(sid))
        assert res.get("token_spent") is False
        assert _token_exists(sid), "an unresolvable root is not a rejected artifact"

    @pytest.mark.asyncio
    async def test_retry_with_a_root_then_advances_on_the_same_token(self, tmp_path):
        """Because the token survived, the user does not have to re-approve."""
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        first = await _advance(sid, confirmation_source="explicit", token=tok)
        assert first.get("advanced") is False
        second = await _advance(
            sid, confirmation_source="explicit", token=tok,
            project_root=_planning_project(tmp_path),
        )
        assert second.get("phase") == "testing", second

    @pytest.mark.asyncio
    async def test_no_pending_gate_validates_nothing(self):
        """A no-op advance must not be turned into a root error by the new check."""
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid, gates_approved=["phase-a", "test-skeletons"], current_phase="implementation")
        tok = _token(sid)
        res = await _advance(sid, confirmation_source="explicit", token=tok)
        assert res.get("advanced") is False
        assert res.get("reason") == "No pending gate to advance"
        assert "error" not in res
        assert _token_exists(sid), "a no-op must not consume the token"


# --------------------------------------------------------------------------- #
# 4. the response names what was accepted
# --------------------------------------------------------------------------- #
class TestAdvanceReportsWhatItValidated:
    @pytest.mark.asyncio
    async def test_success_names_root_and_plan(self, tmp_path):
        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        root = _planning_project(tmp_path)
        res = await _advance(sid, confirmation_source="explicit", token=tok, project_root=root)
        assert res.get("project_root") == root
        assert res.get("validated") == os.path.join(root, "plan.md")
        assert res.get("root_tier") == "explicit"

    @pytest.mark.asyncio
    async def test_hook_renders_the_validated_line(self, tmp_path):
        """gate_advance_outcome turns those fields into the hook's user-facing line."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gao",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "bin", "lib", "gate_advance_outcome.py"),
        )
        gao = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gao)

        sid = f"vp-{uuid.uuid4().hex[:8]}"
        _seed(sid)
        tok = _token(sid)
        root = _planning_project(tmp_path)
        res = await _advance(sid, confirmation_source="explicit", token=tok, project_root=root)

        import json

        classified = gao.classify(json.dumps(res))
        assert classified["outcome"] == "advanced"
        assert os.path.join(root, "plan.md") in classified["validated"]


def _read_token(session_id: str) -> str:
    # LINE ONE ONLY: the token file also carries the gate it authorizes and the plan
    # fingerprint, and the secret is line 1 (read_gate_token's contract). Returning the
    # whole file here would hand the route a "token" with the binding text glued on and
    # fail the presence check for a reason that has nothing to do with the test's subject.
    with open(gate_token_path(session_id)) as fh:
        return fh.readline().strip()
