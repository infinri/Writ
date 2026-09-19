"""tests/test_review_promote_authority.py

Pins defect 1 (plan.md): `writ review --promote` elevates a rule's authority from
`ai-provisional` to `ai-promoted` behind an interactive `typer.confirm`, which
`echo y |` satisfies: an authority change that feeds retrieval ranking, with no
human present, while its sibling operation (candidate to canon) has required the
agent-unforgeable gate token since cycle 1. This module pins the fix: the confirm is
REPLACED by that same token, bound to the ONE rule `writ review <rule_id>
--session-id <sid>` surfaced.

RED today: `writ review` has no `--session-id` / `--token` options; the promote path
still calls `typer.confirm`; `writ.session.gate_token.mint_gate_token` has no
`rule_id` parameter and `claim_gate_token` cannot be given one; none of
`agent_self_approval_blocked` (`event_target="review_promote"`),
`rule_promotion_gate_bound`, `gate_token_unbound`, `gate_token_rule_mismatch`,
`rule_promoted`, or `rule_promotion_claim_lost` are emitted by the CLI; the session
cache has no `pending_review_rule_id` field and `/session/{id}/promotion-review`
does not clear it.

NOT STATED VERBATIM IN plan.md, committed to here as the narrowest reading of its
Analysis section (same disclosure convention as tests/test_gate_token_binding.py's
module docstring):
  * `writ review <rule_id> --session-id <sid>` (no `--promote`) is the surfacing/
    inspect call: it records `pending_review_rule_id` in the session cache ONLY when
    `existing["authority"] == "ai-provisional"`, and clears `pending_candidate_id`
    unconditionally when it runs (mirrors the promotion-review route's own
    exclusivity rule in the other direction).
  * With no `--session-id`, resolution falls back to
    `writ.session.cache.resolve_current_session_id()`; unresolvable refuses with
    exit code 2 naming `--session-id`.
  * `rule_promoted`'s audit row and the cache clear of `pending_review_rule_id`
    happen only after `authoring.promote(db, rule_id, existing)` succeeds.

Per ENF-SYS-005 (plan.md ## Rules Applied): `TestConcurrentPromotionsRealProcesses`
drives the real `writ review --promote` CLI command from REAL OS child processes
against a REAL token file on `/tmp`, because the rename-mutex is a cross-process guarantee
that a mocked `claim_gate_token` or a same-process thread race cannot prove (mirrors
tests/test_gate_token_binding.py's own `TestConcurrentClaimsRealProcesses`). The
Neo4j authority write is the one thing stubbed EVERYWHERE in this module (a fake db
exposing only `get_rule` / `update_rule_authority` / `update_rule_confidence` /
`delete_rule` as bare async recorders): every test here proves the AUTHORIZATION
decision and the call it does or does not make, never the graph mutation itself,
which tests/test_phase6_promote.py already owns.

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
"""

from __future__ import annotations

import contextlib
import os
import socket
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from writ.cli import app

runner = CliRunner()

# Opts out of the autouse WRIT_FRICTION_LOG redirect in tests/conftest.py, for the same
# reason tests/firedrill/conftest.py does: that variable collapses every typed stream into a
# single file, read_streams does not honour it, and these tests assert that each refusal row
# landed on the AUDIT stream specifically. Each test sets WRIT_LOG_ROOT and WRIT_LOG_PROJECT
# into its own tmp_path instead, so isolation is not lost, only relocated.
pytestmark = pytest.mark.no_friction_isolation


def _sid(label: str) -> str:
    return f"reviewpromote-{label}-{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _mint_cleanup(sid: str):
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
    """Mirrors tests/test_gate_token_binding.py's identical fixture, sharing its one
    decision: a leaked file under the real /tmp is not cosmetic, it is a stray
    credential, so `confined_leak_sweep` removes and fails on every file whose session
    id could NOT belong to a live session -- and returns the rest as `left_alone`,
    untouched and reported, because deleting one would burn an approval a human may
    have typed in another window while this ran."""
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


def _read_stream_rows(project: str, stream: str) -> list[dict]:
    from writ.shared.logging import read_streams

    return read_streams(project, [stream])


class _FakeReviewDB:
    """A bare async recorder (mirrors tests/test_authoring_extraction.py's
    _FakeReviewDB), plus `get_rule`, which the CLI's `review()` calls before any of
    promote/reject/downweight/inspect branch, so every test here needs it stubbed."""

    def __init__(self, authority: str = "ai-provisional") -> None:
        self._authority = authority
        self.update_rule_authority = AsyncMock()
        self.update_rule_confidence = AsyncMock()
        self.delete_rule = AsyncMock()

    async def get_rule(self, rule_id: str) -> dict:
        return {
            "rule_id": rule_id, "authority": self._authority, "domain": "Testing",
            "severity": "high", "confidence": "speculative",
            "trigger": "a fixture trigger", "statement": "a fixture statement",
        }


def _make_fake_writ_db(authority: str = "ai-provisional", fake: "_FakeReviewDB | None" = None):
    """Return (factory, fake). `factory` is a zero-arg `@asynccontextmanager`
    function with the SAME shape as `writ.cli._writ_db`, so
    `patch("writ.cli._writ_db", new=factory)` swaps it in exactly as
    tests/test_cli_recall.py's `_fake_writ_db` does for `writ recall`."""
    db = fake if fake is not None else _FakeReviewDB(authority)

    @contextlib.asynccontextmanager
    async def _writ_db():
        yield db

    return _writ_db, db


# ---------------------------------------------------------------------------
# Capabilities 18-19: no token at all refuses; a piped confirm is not a substitute
# ---------------------------------------------------------------------------


class TestPromoteRequiresToken:
    def test_promote_with_no_token_refuses_authority_unchanged_and_logs_self_approval_blocked(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "review-promote-no-token")
        sid = _sid("no-token")
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-TEST-001", "--promote", "--session-id", sid])

        assert result.exit_code != 0, result.output
        fake_db.update_rule_authority.assert_not_awaited()
        fake_db.update_rule_confidence.assert_not_awaited()

        rows = _read_stream_rows("review-promote-no-token", "audit")
        matches = [r for r in rows if r.get("event") == "agent_self_approval_blocked"]
        assert matches, f"expected an agent_self_approval_blocked audit row; got {rows}"
        assert matches[0].get("event_target") == "review_promote", matches[0]

    def test_a_piped_yes_confirmation_does_not_authorize_promotion(self, tmp_path, monkeypatch):
        """`echo y | writ review <rule> --promote`: a piped confirm answers a
        `typer.confirm` prompt that no longer exists on this path; it must not be
        read as authorization by any other means."""
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "review-promote-piped-yes")
        sid = _sid("piped-yes")
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(
                app, ["review", "ENF-TEST-001", "--promote", "--session-id", sid], input="y\n",
            )

        assert result.exit_code != 0, result.output
        fake_db.update_rule_authority.assert_not_awaited()


# ---------------------------------------------------------------------------
# Capabilities 20-22: the three binding refusal classes
# ---------------------------------------------------------------------------


class TestPromoteBindingRefusals:
    RULE_ID = "ENF-TEST-777"

    def _invoke(self, monkeypatch, tmp_path, sid: str, token: str, log_project: str):
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", log_project)
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(
                app, ["review", self.RULE_ID, "--promote", "--session-id", sid, "--token", token],
            )
        return result, fake_db

    def test_a_token_bound_to_a_phase_gate_refuses_with_rule_promotion_gate_bound(
        self, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("gatebound")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="phase-a", plan_hash="abc123def456", rule_id=self.RULE_ID)
            result, fake_db = self._invoke(monkeypatch, tmp_path, sid, token, "review-promote-gatebound")

            assert result.exit_code != 0, result.output
            fake_db.update_rule_authority.assert_not_awaited()
            assert os.path.exists(gate_token_path(sid)), (
                "a token bound to a real phase gate is the user's genuine phase approval "
                "and must not be consumed by an unrelated promotion attempt"
            )
            rows = _read_stream_rows("review-promote-gatebound", "audit")
            assert any(r.get("event") == "rule_promotion_gate_bound" for r in rows), rows

    def test_a_pre_binding_one_line_token_refuses_with_gate_token_unbound(
        self, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path

        sid = _sid("unbound")
        with _mint_cleanup(sid):
            token = uuid.uuid4().hex
            with open(gate_token_path(sid), "w") as f:
                f.write(token + "\n")  # exactly what the pre-cycle writer wrote

            result, fake_db = self._invoke(monkeypatch, tmp_path, sid, token, "review-promote-unbound")

            assert result.exit_code != 0, result.output
            fake_db.update_rule_authority.assert_not_awaited()
            assert os.path.exists(gate_token_path(sid))
            rows = _read_stream_rows("review-promote-unbound", "audit")
            assert any(r.get("event") == "gate_token_unbound" for r in rows), rows

    def test_a_token_bound_to_a_different_rule_refuses_with_gate_token_rule_mismatch(
        self, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path, mint_gate_token

        sid = _sid("rulemismatch")
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="", rule_id="ENF-OTHER-999")
            result, fake_db = self._invoke(monkeypatch, tmp_path, sid, token, "review-promote-rulemismatch")

            assert result.exit_code != 0, result.output
            fake_db.update_rule_authority.assert_not_awaited()
            assert os.path.exists(gate_token_path(sid)), (
                "a rule-mismatch refusal must not consume a token that authorizes a "
                "DIFFERENT rule's promotion"
            )
            rows = _read_stream_rows("review-promote-rulemismatch", "audit")
            assert any(r.get("event") == "gate_token_rule_mismatch" for r in rows), rows


# ---------------------------------------------------------------------------
# Capability 23: the successful bound promotion
# ---------------------------------------------------------------------------


class TestSuccessfulBoundPromotion:
    def test_a_token_bound_to_this_rule_promotes_it_end_to_end(self, tmp_path, monkeypatch):
        from writ.session.cache import _read_cache, _write_cache
        from writ.session.gate_token import gate_token_path, mint_gate_token

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        rule_id = "ENF-TEST-BOUND-001"
        sid = _sid("bound-success")
        cache = _read_cache(sid)
        cache["pending_review_rule_id"] = rule_id
        _write_cache(sid, cache)

        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="", rule_id=rule_id)
            monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
            monkeypatch.setenv("WRIT_LOG_PROJECT", "review-promote-success")
            factory, fake_db = _make_fake_writ_db("ai-provisional")
            with patch("writ.cli._writ_db", new=factory):
                result = runner.invoke(
                    app, ["review", rule_id, "--promote", "--session-id", sid, "--token", token],
                )

            assert result.exit_code == 0, result.output
            fake_db.update_rule_authority.assert_awaited_once_with(rule_id, "ai-promoted")
            fake_db.update_rule_confidence.assert_awaited_once_with(rule_id, "peer-reviewed")
            assert not os.path.exists(gate_token_path(sid)), (
                "a spent approval must not remain on disk"
            )

        rows = _read_stream_rows("review-promote-success", "audit")
        assert any(r.get("event") == "rule_promoted" for r in rows), rows

        after_cache = _read_cache(sid)
        assert after_cache.get("pending_review_rule_id") in (None, ""), (
            f"pending_review_rule_id must be cleared after a successful promotion: {after_cache}"
        )


# ---------------------------------------------------------------------------
# Capability 24: the concurrent claim, real OS processes (ENF-SYS-005)
# ---------------------------------------------------------------------------


def _promote_worker(rule_id: str, sid: str, token: str, result_queue) -> None:
    """Runs the FULL `writ review --promote` CLI command in a CHILD PROCESS against
    the REAL on-disk token file. The rename-mutex (writ.session.gate_token's
    `_claim_file`) is a cross-process guarantee that threads sharing a GIL, or a
    patched `claim_gate_token`, cannot prove; see
    tests/test_gate_token_binding.py's own `_claim_worker` for the identical
    argument. The Neo4j write is stubbed (this module's declared ENF-SYS-005
    exception): this worker proves which concurrent invocation is AUTHORIZED to
    reach `update_rule_authority`, not the graph mutation itself.
    """
    from unittest.mock import AsyncMock
    from contextlib import asynccontextmanager

    from typer.testing import CliRunner as _CliRunner

    import writ.cli as cli_module

    class _FakeDB:
        def __init__(self) -> None:
            self.update_rule_authority = AsyncMock()
            self.update_rule_confidence = AsyncMock()

        async def get_rule(self, rid: str) -> dict:
            return {"rule_id": rid, "authority": "ai-provisional"}

    fake_db = _FakeDB()

    @asynccontextmanager
    async def _writ_db():
        yield fake_db

    from unittest.mock import patch as _patch

    with _patch.object(cli_module, "_writ_db", new=_writ_db):
        result = _CliRunner().invoke(
            cli_module.app,
            ["review", rule_id, "--promote", "--session-id", sid, "--token", token],
        )
    result_queue.put((result.exit_code, fake_db.update_rule_authority.await_count > 0))


class TestConcurrentPromotionsRealProcesses:
    def test_exactly_one_of_several_real_process_promotions_writes_authority(
        self, tmp_path, monkeypatch,
    ):
        import multiprocessing

        from writ.session.gate_token import mint_gate_token

        rule_id = "ENF-CONCURRENT-PROMOTE-001"
        sid = _sid("concurrent-promote")
        # Set BEFORE the fork: the workers are forked, so they inherit this environment
        # and their audit rows land in the same isolated stream this test reads back.
        log_project = "review-promote-concurrent"
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", log_project)
        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="", rule_id=rule_id)

            n = 6
            ctx = multiprocessing.get_context("fork")
            result_queue = ctx.Queue()
            procs = [
                ctx.Process(target=_promote_worker, args=(rule_id, sid, token, result_queue))
                for _ in range(n)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=30)

            exit_codes = [p.exitcode for p in procs]
            assert all(code == 0 for code in exit_codes), (
                f"a worker process exited abnormally: {exit_codes}"
            )

            outcomes = [result_queue.get(timeout=5) for _ in range(n)]
            wrote_count = sum(1 for _cli_exit, wrote_authority in outcomes if wrote_authority)
            assert wrote_count == 1, (
                f"exactly one concurrent promotion must reach update_rule_authority; "
                f"got {outcomes}"
            )

            # THE LOSERS MUST SAY SO IN THE AUDIT STREAM, not just fail quietly. This
            # cycle's own STREAM_MAP change argues that a fail-closed refusal sharing an
            # event name with an absent one is indistinguishable from it in the log, and
            # `rule_promotion_claim_lost` is the class with the least other coverage: five
            # of six processes take this path on every run and nothing pinned that they
            # record it. Asserting only "exactly one wrote" would pass just as well if the
            # other five vanished silently.
            rows = _read_stream_rows(log_project, "audit")
            assert rows, "the audit stream is empty, so the next assertion would be vacuous"

            # WHICH ARM REFUSES A LOSER, measured rather than assumed. The first draft of
            # this assertion expected `rule_promotion_claim_lost` and got zero of them:
            # the losers are refused EARLIER, at the token read, because the winner's
            # rename already consumed the file so the expected secret reads empty. So
            # `rule_promotion_claim_lost` covers only the narrow window where the file
            # still exists at read time and the rename then loses, which six forked
            # processes do not reliably hit. Asserting it here would have been a test that
            # passes only by luck.
            #
            # The property that actually matters is the one this cycle argues for: EVERY
            # loser is refused AND SAYS SO in the audit stream, whichever arm caught it.
            refusal_events = {"agent_self_approval_blocked", "rule_promotion_claim_lost"}
            refusals = [r for r in rows if r.get("event") in refusal_events]
            promoted = [r for r in rows if r.get("event") == "rule_promoted"]
            assert len(promoted) == 1, (
                f"exactly one promotion must be recorded, got {len(promoted)}"
            )
            assert len(refusals) == n - 1, (
                f"expected {n - 1} recorded refusals, one per losing process, got "
                f"{len(refusals)}; events present: "
                f"{sorted({r.get('event') for r in rows})}"
            )
            assert all(r.get("rule_id") == rule_id for r in refusals), (
                f"every refusal row must name the rule it was refused for: {refusals}"
            )


# ---------------------------------------------------------------------------
# Capability 25: writ review <rule> --session-id surfaces and clears the other pending
# ---------------------------------------------------------------------------


class TestReviewInspectSurfacesRuleAndClearsCandidate:
    def test_surfacing_an_ai_provisional_rule_records_it_and_clears_pending_candidate(
        self, tmp_path, monkeypatch,
    ):
        from writ.session.cache import _read_cache, _write_cache

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        sid = _sid("surface")
        cache = _read_cache(sid)
        cache["pending_candidate_id"] = "CAND-STALE-1"
        _write_cache(sid, cache)

        factory, _fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-SURFACE-001", "--session-id", sid])

        assert result.exit_code == 0, result.output
        after = _read_cache(sid)
        assert after.get("pending_review_rule_id") == "ENF-SURFACE-001", after
        assert after.get("pending_candidate_id") in (None, ""), (
            f"surfacing a rule must clear a pending candidate surfacing: {after}"
        )

    def test_a_non_ai_provisional_rule_is_inspected_but_not_recorded_as_surfaced(
        self, tmp_path, monkeypatch,
    ):
        from writ.session.cache import _read_cache

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        sid = _sid("nosurface")
        factory, _fake_db = _make_fake_writ_db("human")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-HUMAN-001", "--session-id", sid])

        assert result.exit_code == 0, result.output
        after = _read_cache(sid)
        assert after.get("pending_review_rule_id") in (None, ""), (
            f"a surfacing that could not legally be promoted must leave no binding "
            f"behind: {after}"
        )


# ---------------------------------------------------------------------------
# Capability 26: /session/{id}/promotion-review clears a pending rule surfacing
# ---------------------------------------------------------------------------


class TestPromotionReviewRouteClearsPendingRuleSurfacing:
    @pytest.mark.asyncio
    async def test_recording_a_candidate_clears_a_pending_rule_surfacing(
        self, tmp_path, monkeypatch,
    ):
        import writ.server as server_module
        from writ.server import SessionPromotionReviewRequest
        from writ.server.routes.gate import session_promotion_review
        from writ.session.cache import _read_cache, _write_cache

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        sid = _sid("route-clears-rule")
        cache = _read_cache(sid)
        cache["pending_review_rule_id"] = "ENF-STALE-RULE-1"
        _write_cache(sid, cache)

        monkeypatch.setattr(server_module, "_db", object())
        monkeypatch.setattr(server_module, "_pipeline", object())

        async def _fake_artifact(candidate_id, pipeline, db):
            return {"statement": "s", "trigger": "t"}

        monkeypatch.setattr(server_module, "build_promotion_review_artifact", _fake_artifact)

        result = await session_promotion_review(
            sid, SessionPromotionReviewRequest(candidate_id="CAND-NEW-1"),
        )

        assert result.get("candidate_id") == "CAND-NEW-1", result
        after = _read_cache(sid)
        assert after.get("pending_candidate_id") == "CAND-NEW-1", after
        assert after.get("pending_review_rule_id") in (None, ""), (
            f"recording a candidate must clear a pending rule surfacing: {after}"
        )


# ---------------------------------------------------------------------------
# Capability 27: unresolvable session
# ---------------------------------------------------------------------------


class TestPromoteUnresolvableSession:
    def test_no_session_id_and_no_resolvable_session_refuses_exit_2_names_the_flag_and_logs_nothing(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "review-promote-nosession")
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-TEST-001", "--promote"])

        assert result.exit_code == 2, result.output
        assert "--session-id" in result.output
        fake_db.update_rule_authority.assert_not_awaited()
        rows = _read_stream_rows("review-promote-nosession", "friction")
        assert rows == [], f"an unresolvable session must write no friction row; got {rows}"


# ---------------------------------------------------------------------------
# Capability 28: daemon-unreachable equivalence
# ---------------------------------------------------------------------------


class TestDaemonUnreachableEquivalence:
    def test_the_whole_refuse_then_authorize_then_promote_sequence_is_unaffected(
        self, tmp_path, monkeypatch,
    ):
        """writ review never speaks to the daemon (plan.md: 'the CLI must work with
        the daemon down'), so pointing WRIT_PORT at a closed port and disabling
        autostart must not change any step of this sequence."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        monkeypatch.setenv("WRIT_PORT", str(closed_port))
        monkeypatch.setenv("WRIT_NO_AUTOSTART", "1")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()

        from writ.session.gate_token import mint_gate_token

        rule_id = "ENF-DAEMON-DOWN-001"
        sid = _sid("daemon-down")
        factory, fake_db = _make_fake_writ_db("ai-provisional")

        with patch("writ.cli._writ_db", new=factory):
            refused = runner.invoke(app, ["review", rule_id, "--promote", "--session-id", sid])
        assert refused.exit_code != 0, refused.output
        fake_db.update_rule_authority.assert_not_awaited()

        with patch("writ.cli._writ_db", new=factory):
            surfaced = runner.invoke(app, ["review", rule_id, "--session-id", sid])
        assert surfaced.exit_code == 0, surfaced.output

        with _mint_cleanup(sid):
            token = mint_gate_token(sid, gate="", plan_hash="", rule_id=rule_id)
            with patch("writ.cli._writ_db", new=factory):
                promoted = runner.invoke(
                    app, ["review", rule_id, "--promote", "--session-id", sid, "--token", token],
                )
            assert promoted.exit_code == 0, promoted.output
            fake_db.update_rule_authority.assert_awaited_once_with(rule_id, "ai-promoted")


# ---------------------------------------------------------------------------
# Capability 29: --reject and --downweight are unaffected
# ---------------------------------------------------------------------------


class TestRejectAndDownweightUnaffected:
    """Out of scope, deliberately (plan.md): neither elevates authority, so neither
    gains a token requirement. Regression pins, not new behavior."""

    def test_reject_still_uses_the_interactive_confirm_and_deletes_on_yes(self) -> None:
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-REJECT-001", "--reject"], input="y\n")
        assert result.exit_code == 0, result.output
        fake_db.delete_rule.assert_awaited_once_with("ENF-REJECT-001")

    def test_reject_declines_on_no_and_deletes_nothing(self) -> None:
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-REJECT-002", "--reject"], input="n\n")
        assert result.exit_code == 0, result.output
        fake_db.delete_rule.assert_not_awaited()

    def test_downweight_still_uses_the_interactive_confirm_with_no_token_required(self) -> None:
        factory, fake_db = _make_fake_writ_db("human")  # downweight has NO authority guard
        with patch("writ.cli._writ_db", new=factory):
            result = runner.invoke(app, ["review", "ENF-DOWNWEIGHT-001", "--downweight"], input="y\n")
        assert result.exit_code == 0, result.output
        fake_db.update_rule_confidence.assert_awaited_once_with("ENF-DOWNWEIGHT-001", "speculative")
