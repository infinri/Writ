"""Program item 6 (attribution and trust records), capabilities 28 to 41: the review actions.

`writ review <rule_id>` gains two token-gated actions beside --promote:

    --dispute   marks a rule disputed (never adjudicated), any authority
    --verify    restarts the rule's verify clock, any authority

and every approval-shaped write (promote, dispute, verify, and the candidate route's
promotion) records WHO approved on a TrustEvent, from the gate token's lines 6 and 7.

What this file pins, each against the seam that owns it:

  * surfacing (`--dispute` or `--verify` without --token) records the ACTION-QUALIFIED
    binding `dispute:X` / `verify:X`, writes nothing and does not notify the daemon (28);
  * the guarded write: rule props, one TrustEvent carrying the token's identity, one audit
    row, the token consumed, the surfacing cleared, the daemon notified (29, 30);
  * an approval for one action cannot be spent on another (31), the refusal classes are
    --promote's (32), --promote keeps its guard order and gains the approval record (33);
  * identity comes from the token and nowhere else (34), a pre-identity token still
    authorizes with empty identity (36), reject is untouched (37);
  * the candidate route's promotion records its approval too (38), and a failed approval
    record leaves the promotion committed on both paths (39);
  * the audit stream mapping, and no identity in any audit or friction row (40);
  * inspect shows the trust props and still surfaces only ai-provisional rules (41).

REAL GRAPH, REAL FILES, REAL PROCESSES (ENF-SYS-005). Statement counts ("exactly 3", "exactly
5"), the property writes and the TrustEvent contents are claims about what the graph holds, so
they run against the isolated instance with real token files; the one-approval-one-action
claim runs real concurrent `writ review` processes. The fake database of
tests/test_review_promote_authority.py is confined to guard ordering, refusal messages,
surfacing and the failure branch of the approval record.

Identity values are explicit fixed strings minted into the token. Nothing here reads the
real machine's login or git name.

RED today: `writ review` has no --dispute, --verify or --note; writ.session.gate_token has
no read_gate_identity or rule_action_binding; writ.authoring has no record_approval; the
graph has no TrustEvent writer; rule_disputed and rule_verified are not in STREAM_MAP.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from tests._writ_cmd import WRIT_CMD_PREFIX
from tests.test_review_promote_authority import (  # noqa: F401  (autouse leak guard + helpers)
    _FakeReviewDB,
    _make_fake_writ_db,
    _mint_cleanup,
    _no_leaked_gate_tokens,
    _read_stream_rows,
    _sid,
)
from writ.cli import app

# Same reason as tests/test_review_promote_authority.py: the audit assertions read the typed
# streams, which the single-file collapse of WRIT_FRICTION_LOG would defeat.
pytestmark = pytest.mark.no_friction_isolation

runner = CliRunner()

LOGIN = "alice-fixed"
GIT_NAME = "Alice Fixed Name"
NOTE = "the retrieval claim here is contradicted by the migration plan"
ALL_STREAMS = ("audit", "friction", "metrics", "errors")
OLD_VERIFIED = "2020-01-01"


# ---------------------------------------------------------------------------
# Helpers: the real graph, token files, the CLI
# ---------------------------------------------------------------------------


def _graph(work):
    """Run `async work(db)` against the isolated graph on its own loop and close it.

    A sync helper on purpose: `writ review` calls asyncio.run itself, so a test that drives
    it cannot already be inside a running loop (tests/_graph.py::count makes the same call).
    """
    from tests._graph import connection

    async def _go():
        db = connection()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _rule_data(rule_id: str, authority: str) -> dict:
    return {
        "rule_id": rule_id, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "When the trust review fixture is read.",
        "statement": "A fixture statement for the trust review tests.",
        "violation": "A fixture violation.", "pass_example": "A fixture pass example.",
        "enforcement": "Reviewed in the per-slice findings table.",
        "rationale": "A fixture rationale.", "last_validated": "2026-03-15",
        "authority": authority,
    }


async def _delete_rule_and_events(db, rule_id: str) -> None:
    await db._run("MATCH (r:Rule {rule_id: $rid}) DETACH DELETE r", rid=rule_id)
    await db._run("MATCH (e:TrustEvent {rule_id: $rid}) DETACH DELETE e", rid=rule_id)


@pytest.fixture()
def graph_rule():
    """`make(label, authority, **graph_props) -> rule_id`: a real Rule on the isolated graph.

    TrustEvent nodes are preserved by clear_all (they are records), so teardown deletes this
    test's own rules AND events by rule id instead of trusting a wipe.
    """
    made: list[str] = []

    def make(label: str, authority: str = "human", **graph_props) -> str:
        rid = f"TRUSTREV-{label.upper()}-{100 + uuid.uuid4().int % 900}"

        async def _seed(db):
            await _delete_rule_and_events(db, rid)
            await db.create_rule(_rule_data(rid, authority), source_origin="graph-authored")
            if graph_props:
                await db._run(
                    "MATCH (r:Rule {rule_id: $rid}) SET r += $props RETURN count(r) AS c",
                    rid=rid, props=graph_props,
                )

        _graph(_seed)
        made.append(rid)
        return rid

    yield make

    async def _drop(db):
        for rid in made:
            await _delete_rule_and_events(db, rid)

    if made:
        _graph(_drop)


def _rule_node(rule_id: str) -> dict:
    return _graph(lambda db: db.get_rule(rule_id))


def _trust_events(rule_id: str) -> list[dict]:
    async def _read(db):
        rows = await db._run(
            "MATCH (e:TrustEvent {rule_id: $rid}) RETURN e ORDER BY e.ts", rid=rule_id,
        )
        return [dict(r["e"]) for r in rows]

    return _graph(_read)


@contextlib.contextmanager
def _count_statements():
    """Count every statement the connection issues through its three query helpers.

    The three helpers are the only doors the review path uses (writ/graph/db/_query_runner.py),
    so a count taken here is the count the graph saw. The patch is on the shared mixin and is
    restored in a finally.
    """
    from writ.graph.db._query_runner import _QueryRunnerMixin

    state = {"n": 0, "queries": []}
    names = ("_run", "_run_single", "_write_single")
    originals = {name: getattr(_QueryRunnerMixin, name) for name in names}

    def _wrap(original):
        async def counted(self, query, **params):
            state["n"] += 1
            state["queries"].append(" ".join(str(query).split())[:80])
            return await original(self, query, **params)

        return counted

    for name in names:
        setattr(_QueryRunnerMixin, name, _wrap(originals[name]))
    try:
        yield state
    finally:
        for name in names:
            setattr(_QueryRunnerMixin, name, originals[name])


def _env(monkeypatch, tmp_path, label: str) -> str:
    """Isolate cache and logs for one test; returns the log project name."""
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    project = f"trust-review-{label}"
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache))
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", project)
    return project


def _all_rows(project: str) -> list[dict]:
    from writ.shared.logging import read_streams

    return read_streams(project, list(ALL_STREAMS))


def _events(project: str, stream: str) -> list[str]:
    return [r.get("event") for r in _read_stream_rows(project, stream)]


def _pending_binding(sid: str) -> str:
    from writ.session.cache import _read_cache

    return _read_cache(sid).get("pending_review_rule_id") or ""


def _cli(args, *, env=None, db_factory=None):
    """Run `writ review ...` with the daemon notice patched; returns (result, notify_mock)."""
    with patch("writ.cli._notify_daemon_reload") as notify:
        if db_factory is not None:
            with patch("writ.cli._writ_db", new=db_factory):
                result = runner.invoke(app, args, env=env)
        else:
            result = runner.invoke(app, args, env=env)
    return result, notify


def _surface(action: str, rule_id: str, sid: str, *, db_factory=None):
    """The agent's surfacing step. Returns the binding the session cache now holds."""
    args = ["review", rule_id, "--session-id", sid]
    if action != "promote":
        args.insert(2, f"--{action}")
    result, notify = _cli(args, db_factory=db_factory)
    assert result.exit_code == 0, result.output
    notify.assert_not_called()
    return _pending_binding(sid)


def _mint(sid: str, binding: str, *, os_login: str = LOGIN, git_name: str = GIT_NAME,
          gate: str = "") -> str:
    """What the approval hook does after THE USER replies "approved": mint line 5 verbatim
    from the surfaced binding, with the identity the hook process captured."""
    from writ.session.gate_token import mint_gate_token

    return mint_gate_token(
        sid, gate=gate, plan_hash="", rule_id=binding, os_login=os_login, git_name=git_name,
    )


def _action_args(action: str, rule_id: str, sid: str, token: str, *, note: str | None = None):
    args = ["review", rule_id, f"--{action}", "--session-id", sid, "--token", token]
    if note is not None:
        args += ["--note", note]
    return args


def _assert_no_identity_in_any_log(project: str) -> None:
    rows = _all_rows(project)
    assert rows, "no log rows at all, so the absence assertion below would be vacuous"
    for row in rows:
        dumped = json.dumps(row)
        assert LOGIN not in dumped, row
        assert GIT_NAME not in dumped, row
        assert "os_login" not in row and "git_name" not in row, row


class _TrustFakeDB(_FakeReviewDB):
    """The shared fake, with trust props on the rule `get_rule` returns."""

    def __init__(self, authority: str = "human", **trust_props) -> None:
        super().__init__(authority)
        self._trust_props = trust_props

    async def get_rule(self, rule_id: str) -> dict:
        rule = await super().get_rule(rule_id)
        rule.update(self._trust_props)
        return rule


# ---------------------------------------------------------------------------
# Capability 28: surfacing
# ---------------------------------------------------------------------------


class TestSurfacingDisputeAndVerify:
    @pytest.mark.parametrize("action", ["dispute", "verify"])
    @pytest.mark.parametrize("authority", ["human", "ai-provisional", "ai-promoted"])
    def test_surfacing_records_the_qualified_binding_for_a_rule_of_any_authority(
        self, action, authority, tmp_path, monkeypatch,
    ):
        from writ.session.cache import _read_cache, _write_cache

        _env(monkeypatch, tmp_path, f"surface-{action}")
        sid = _sid(f"surface-{action}")
        cache = _read_cache(sid)
        cache["pending_candidate_id"] = "CAND-STALE-1"
        _write_cache(sid, cache)
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB(authority))

        result, notify = _cli(
            ["review", "ENF-SURFACE-001", f"--{action}", "--session-id", sid],
            db_factory=factory,
        )

        assert result.exit_code == 0, result.output
        after = _read_cache(sid)
        assert after.get("pending_review_rule_id") == f"{action}:ENF-SURFACE-001", after
        assert after.get("pending_candidate_id") in (None, ""), after
        assert "approved" in result.output, "the next steps must tell the user what to reply"
        assert "--token" in result.output, "and how the approved action is re-run"
        fake_db.set_rule_trust_props.assert_not_awaited()
        fake_db.create_trust_event.assert_not_awaited()
        fake_db.update_rule_authority.assert_not_awaited()
        notify.assert_not_called()

    def test_plain_inspect_still_records_the_bare_rule_id_for_promotion(self, tmp_path, monkeypatch):
        """The existing pin, restated next to the new surfacings: inspect (no flag) binds the
        bare rule id, which is what --promote compares against."""
        _env(monkeypatch, tmp_path, "surface-promote")
        sid = _sid("surface-promote")
        factory, _fake = _make_fake_writ_db("ai-provisional")
        assert _surface("promote", "ENF-SURFACE-002", sid, db_factory=factory) == "ENF-SURFACE-002"

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_surfacing_on_the_real_graph_issues_one_statement_and_writes_nothing(
        self, action, graph_rule, tmp_path, monkeypatch,
    ):
        _env(monkeypatch, tmp_path, f"surface-real-{action}")
        rid = graph_rule(f"surf{action[:3]}", "human")
        sid = _sid("surface-real")
        before = _rule_node(rid)

        with _count_statements() as counted:
            binding = _surface(action, rid, sid)

        assert binding == f"{action}:{rid}"
        assert counted["n"] == 1, counted["queries"]
        assert _rule_node(rid) == before, "surfacing must not touch the rule"
        assert _trust_events(rid) == []


# ---------------------------------------------------------------------------
# Capabilities 29, 34, 40: the dispute, end to end on the real graph
# ---------------------------------------------------------------------------


class TestDisputeEndToEnd:
    def test_a_token_minted_after_surfacing_disputes_the_rule_and_records_who_approved(
        self, graph_rule, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path

        project = _env(monkeypatch, tmp_path, "dispute")
        rid = graph_rule("dispute", "human", last_verified=OLD_VERIFIED)
        sid = _sid("dispute")
        with _mint_cleanup(sid):
            binding = _surface("dispute", rid, sid)
            token = _mint(sid, binding)

            with _count_statements() as counted:
                result, notify = _cli(_action_args("dispute", rid, sid, token, note=NOTE))

            assert result.exit_code == 0, result.output
            assert counted["n"] == 3, counted["queries"]
            assert not os.path.exists(gate_token_path(sid)), "one approval, one action: consumed"
            notify.assert_called_once()

        node = _rule_node(rid)
        assert node["disputed"] is True
        assert node["last_verified"] == OLD_VERIFIED, "a dispute does not restart the verify clock"
        assert node["authority"] == "human", "a dispute never changes authority"

        events = _trust_events(rid)
        assert len(events) == 1, events
        event = events[0]
        assert event["kind"] == "dispute"
        assert event["via"] == "review_dispute"
        assert event["session_id"] == sid
        assert event["os_login"] == LOGIN
        assert event["git_name"] == GIT_NAME
        assert event["note"] == NOTE
        assert event["rule_id"] == rid
        assert event["event_id"].startswith("te-")
        assert event["provenance"] == "record"
        assert event["source_origin"] == "graph-authored"
        assert event["ts"]

        audit = [r for r in _read_stream_rows(project, "audit") if r.get("event") == "rule_disputed"]
        assert len(audit) == 1, audit
        assert audit[0].get("rule_id") == rid
        assert _pending_binding(sid) == "", "the surfacing is cleared once the action is spent"
        _assert_no_identity_in_any_log(project)

    def test_a_dispute_without_a_note_records_an_empty_note(self, graph_rule, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path, "dispute-nonote")
        rid = graph_rule("dispnote", "human")
        sid = _sid("dispute-nonote")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("dispute", rid, sid))
            result, _notify = _cli(_action_args("dispute", rid, sid, token))
        assert result.exit_code == 0, result.output
        assert [e["note"] for e in _trust_events(rid)] == [""]


# ---------------------------------------------------------------------------
# Capability 30: the verify, end to end
# ---------------------------------------------------------------------------


class TestVerifyEndToEnd:
    def test_a_verify_restarts_the_clock_and_records_who_approved(
        self, graph_rule, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path

        project = _env(monkeypatch, tmp_path, "verify")
        rid = graph_rule("verify", "ai-promoted", last_verified=OLD_VERIFIED)
        sid = _sid("verify")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("verify", rid, sid))
            first_day = date.today().isoformat()
            with _count_statements() as counted:
                result, notify = _cli(_action_args("verify", rid, sid, token))
            last_day = date.today().isoformat()

            assert result.exit_code == 0, result.output
            assert counted["n"] == 3, counted["queries"]
            assert not os.path.exists(gate_token_path(sid))
            notify.assert_called_once()

        node = _rule_node(rid)
        assert node["last_verified"] in {first_day, last_day}, node["last_verified"]
        assert not node.get("disputed"), "a verify does not dispute"

        events = _trust_events(rid)
        assert len(events) == 1, events
        assert events[0]["kind"] == "verify"
        assert events[0]["via"] == "review_verify"
        assert events[0]["session_id"] == sid
        assert events[0]["os_login"] == LOGIN
        assert events[0]["git_name"] == GIT_NAME

        audit = [r for r in _read_stream_rows(project, "audit") if r.get("event") == "rule_verified"]
        assert len(audit) == 1, audit
        assert _pending_binding(sid) == ""
        _assert_no_identity_in_any_log(project)


# ---------------------------------------------------------------------------
# Capability 31: an approval for one action is not spendable on another
# ---------------------------------------------------------------------------


class TestActionBindingRefusals:
    RULE_ID = "ENF-BIND-001"

    @pytest.mark.parametrize("bound_action, attempted", [
        ("promote", "dispute"),
        ("promote", "verify"),
        ("dispute", "promote"),
        ("dispute", "verify"),
        ("verify", "dispute"),
        ("verify", "promote"),
    ])
    def test_a_token_bound_to_one_action_is_refused_for_every_other(
        self, bound_action, attempted, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path, rule_action_binding

        project = _env(monkeypatch, tmp_path, f"bind-{bound_action}-{attempted}")
        sid = _sid("bind")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("ai-provisional"))
        with _mint_cleanup(sid):
            token = _mint(sid, rule_action_binding(bound_action, self.RULE_ID))

            result, notify = _cli(
                _action_args(attempted, self.RULE_ID, sid, token), db_factory=factory,
            )

            assert result.exit_code != 0, result.output
            assert os.path.exists(gate_token_path(sid)), (
                "a refused action must leave the approval for the action it WAS given for"
            )
            fake_db.update_rule_authority.assert_not_awaited()
            fake_db.set_rule_trust_props.assert_not_awaited()
            fake_db.create_trust_event.assert_not_awaited()
            notify.assert_not_called()
        assert "gate_token_rule_mismatch" in _events(project, "audit")

    def test_the_matching_action_still_spends_it(self, graph_rule, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path, "bind-match")
        rid = graph_rule("bindok", "human")
        sid = _sid("bind-match")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("verify", rid, sid))
            result, _notify = _cli(_action_args("verify", rid, sid, token))
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Capability 32: the refusal classes of --promote, for dispute and verify
# ---------------------------------------------------------------------------


class TestDisputeAndVerifyRefusals:
    RULE_ID = "ENF-REFUSE-001"

    def _assert_nothing_written(self, fake_db, notify) -> None:
        fake_db.set_rule_trust_props.assert_not_awaited()
        fake_db.create_trust_event.assert_not_awaited()
        fake_db.update_rule_authority.assert_not_awaited()
        notify.assert_not_called()

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_no_token_only_surfaces_and_never_writes(self, action, tmp_path, monkeypatch):
        # Capability 28: without --token the command is the surfacing step (exit 0),
        # so the refusal property here is that nothing is written or recorded as done.
        project = _env(monkeypatch, tmp_path, f"refuse-none-{action}")
        sid = _sid("refuse-none")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("human"))

        result, notify = _cli(
            ["review", self.RULE_ID, f"--{action}", "--session-id", sid], db_factory=factory,
        )

        assert result.exit_code == 0, result.output
        self._assert_nothing_written(fake_db, notify)
        done = {"dispute": "rule_disputed", "verify": "rule_verified"}[action]
        rows = [r for r in _read_stream_rows(project, "audit") if r.get("event") == done]
        assert not rows, rows

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_a_wrong_token_is_refused_and_the_real_approval_survives(
        self, action, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path, rule_action_binding

        project = _env(monkeypatch, tmp_path, f"refuse-wrong-{action}")
        sid = _sid("refuse-wrong")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("human"))
        with _mint_cleanup(sid):
            _mint(sid, rule_action_binding(action, self.RULE_ID))
            result, notify = _cli(
                _action_args(action, self.RULE_ID, sid, "not-the-token"), db_factory=factory,
            )
            assert result.exit_code != 0
            assert os.path.exists(gate_token_path(sid))
            self._assert_nothing_written(fake_db, notify)
        assert "agent_self_approval_blocked" in _events(project, "audit")

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_an_unbound_one_line_token_is_refused_as_unbound(self, action, tmp_path, monkeypatch):
        from writ.session.gate_token import gate_token_path

        project = _env(monkeypatch, tmp_path, f"refuse-unbound-{action}")
        sid = _sid("refuse-unbound")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("human"))
        with _mint_cleanup(sid):
            token = uuid.uuid4().hex
            Path(gate_token_path(sid)).write_text(token + "\n")
            result, notify = _cli(_action_args(action, self.RULE_ID, sid, token), db_factory=factory)
            assert result.exit_code != 0
            assert os.path.exists(gate_token_path(sid))
            self._assert_nothing_written(fake_db, notify)
        assert "gate_token_unbound" in _events(project, "audit")

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_a_phase_gate_bound_token_is_refused_and_not_consumed(self, action, tmp_path, monkeypatch):
        from writ.session.gate_token import gate_token_path, mint_gate_token, rule_action_binding

        project = _env(monkeypatch, tmp_path, f"refuse-gate-{action}")
        sid = _sid("refuse-gate")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("human"))
        with _mint_cleanup(sid):
            token = mint_gate_token(
                sid, gate="phase-a", plan_hash="abc123def456",
                rule_id=rule_action_binding(action, self.RULE_ID),
                os_login=LOGIN, git_name=GIT_NAME,
            )
            result, notify = _cli(_action_args(action, self.RULE_ID, sid, token), db_factory=factory)
            assert result.exit_code != 0
            assert os.path.exists(gate_token_path(sid)), "the user's genuine phase approval"
            self._assert_nothing_written(fake_db, notify)
        assert "rule_promotion_gate_bound" in _events(project, "audit")

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_an_unresolvable_session_exits_two_and_names_the_flag(self, action, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
        project = _env(monkeypatch, tmp_path, f"refuse-nosession-{action}")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB("human"))

        result, notify = _cli(["review", self.RULE_ID, f"--{action}"], db_factory=factory)

        assert result.exit_code == 2, result.output
        assert "--session-id" in result.output
        self._assert_nothing_written(fake_db, notify)
        assert _read_stream_rows(project, "friction") == []

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    @pytest.mark.parametrize("authority", ["human", "ai-promoted", "ai-provisional"])
    def test_a_valid_approval_is_honoured_for_a_rule_of_any_authority(
        self, action, authority, tmp_path, monkeypatch,
    ):
        """Never requires ai-provisional: that legality step is promote's alone."""
        _env(monkeypatch, tmp_path, f"anyauth-{action}")
        sid = _sid("anyauth")
        factory, fake_db = _make_fake_writ_db(fake=_TrustFakeDB(authority))
        with _mint_cleanup(sid):
            token = _mint(sid, _surface(action, self.RULE_ID, sid, db_factory=factory))
            result, notify = _cli(
                _action_args(action, self.RULE_ID, sid, token), db_factory=factory,
            )

        assert result.exit_code == 0, result.output
        fake_db.set_rule_trust_props.assert_awaited_once()
        props = fake_db.set_rule_trust_props.await_args.args[1]
        if action == "dispute":
            assert props["disputed"] is True, props
        else:
            assert props["last_verified"], props
        fake_db.create_trust_event.assert_awaited_once()
        assert fake_db.create_trust_event.await_args.kwargs["kind"] == action
        fake_db.update_rule_authority.assert_not_awaited()
        notify.assert_called_once()


# ---------------------------------------------------------------------------
# Capability 33: --promote keeps its guards and gains the approval record
# ---------------------------------------------------------------------------


class TestPromoteKeepsItsGuardsAndRecordsTheApproval:
    def test_a_non_ai_provisional_rule_is_refused_before_any_credential_check(
        self, tmp_path, monkeypatch,
    ):
        """Guard order is part of the contract: legality comes BEFORE the token step, so
        an illegal transition prints its own message and logs no self-approval row."""
        project = _env(monkeypatch, tmp_path, "promote-legality")
        sid = _sid("promote-legality")
        factory, fake_db = _make_fake_writ_db("human")

        result, notify = _cli(
            ["review", "ENF-LEGAL-001", "--promote", "--session-id", sid], db_factory=factory,
        )

        assert result.exit_code == 1
        assert "Cannot promote: ENF-LEGAL-001 has authority 'human'" in result.output
        assert "agent_self_approval_blocked" not in _events(project, "audit")
        fake_db.update_rule_authority.assert_not_awaited()
        fake_db.create_trust_event.assert_not_awaited()
        notify.assert_not_called()

    def test_a_successful_promotion_sets_the_approval_props_and_writes_one_event_in_five_statements(
        self, graph_rule, tmp_path, monkeypatch,
    ):
        project = _env(monkeypatch, tmp_path, "promote-real")
        rid = graph_rule("promote", "ai-provisional")
        sid = _sid("promote-real")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("promote", rid, sid))
            first_day = date.today().isoformat()
            with _count_statements() as counted:
                result, notify = _cli(_action_args("promote", rid, sid, token))
            last_day = date.today().isoformat()

            assert result.exit_code == 0, result.output
            assert counted["n"] == 5, counted["queries"]
            notify.assert_called_once()

        node = _rule_node(rid)
        assert node["authority"] == "ai-promoted"
        assert node["confidence"] == "peer-reviewed"
        assert node["approval_via"] == "review_promote"
        assert isinstance(node["approved_at"], str) and len(node["approved_at"]) >= 10
        assert node["last_verified"] in {first_day, last_day}, "an approval is a verification"

        events = _trust_events(rid)
        assert len(events) == 1, events
        assert events[0]["kind"] == "approval"
        assert events[0]["via"] == "review_promote"
        assert events[0]["session_id"] == sid
        assert events[0]["os_login"] == LOGIN
        assert events[0]["git_name"] == GIT_NAME
        assert "rule_promoted" in _events(project, "audit")
        _assert_no_identity_in_any_log(project)


# ---------------------------------------------------------------------------
# Capability 34: identity comes from the token, nowhere else
# ---------------------------------------------------------------------------


class TestIdentityComesFromTheToken:
    SPOOF_ENV = {
        "USER": "mallory", "LOGNAME": "mallory", "USERNAME": "mallory",
        "GIT_AUTHOR_NAME": "Mallory Spoof", "GIT_COMMITTER_NAME": "Mallory Spoof",
    }

    @pytest.mark.parametrize("action", ["promote", "dispute", "verify"])
    def test_the_event_carries_the_token_identity_whatever_the_environment_says(
        self, action, graph_rule, tmp_path, monkeypatch,
    ):
        from writ.session.gate_token import gate_token_path

        _env(monkeypatch, tmp_path, f"spoof-{action}")
        authority = "ai-provisional" if action == "promote" else "human"
        rid = graph_rule(f"spf{action[:3]}", authority)
        sid = _sid("spoof")
        gitconfig = tmp_path / "spoof-gitconfig"
        gitconfig.write_text("[user]\n\tname = Mallory Spoof\n")
        env = {**self.SPOOF_ENV, "GIT_CONFIG_GLOBAL": str(gitconfig)}
        with _mint_cleanup(sid):
            token = _mint(sid, _surface(action, rid, sid))
            result, _notify = _cli(_action_args(action, rid, sid, token), env=env)
            assert result.exit_code == 0, result.output
            assert not os.path.exists(gate_token_path(sid))

        events = _trust_events(rid)
        assert len(events) == 1, events
        assert events[0]["os_login"] == LOGIN
        assert events[0]["git_name"] == GIT_NAME

    def test_the_command_exposes_no_way_to_name_an_approver(self):
        result = runner.invoke(app, ["review", "--help"])
        assert result.exit_code == 0
        help_text = result.output.lower()
        for forbidden in ("identity", "os-login", "os_login", "git-name", "git_name",
                          "--user", "--approver", "--as"):
            assert forbidden not in help_text, f"review --help advertises {forbidden!r}"
        for needed in ("--dispute", "--verify", "--note"):
            assert needed in help_text, f"review --help does not mention {needed}"


# ---------------------------------------------------------------------------
# Capability 35: one approval, one action, under real concurrency
# ---------------------------------------------------------------------------


class TestConcurrentDisputesRealProcesses:
    PROCESSES = 4

    def test_exactly_one_of_several_real_dispute_processes_writes(
        self, graph_rule, tmp_path, monkeypatch,
    ):
        """Real `writ review --dispute` processes racing one real token file against the
        isolated graph. The loser's refusal is recorded either at the token read (the
        winner's rename already consumed the file) or at the claim itself
        (rule_promotion_claim_lost); which arm catches a given loser is timing, so the
        assertion is that EVERY loser is refused and says so, and the write happens once."""
        project = _env(monkeypatch, tmp_path, "concurrent-dispute")
        rid = graph_rule("race", "human")
        sid = _sid("race")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("dispute", rid, sid))
            env = {**os.environ, "WRIT_DAEMON_RELOAD": "0"}
            cmd = [*WRIT_CMD_PREFIX, *_action_args("dispute", rid, sid, token)]
            procs = [
                subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(self.PROCESSES)
            ]
            outcomes = [(p.communicate(timeout=180), p.returncode) for p in procs]

        exit_codes = [code for _out, code in outcomes]
        assert exit_codes.count(0) == 1, f"exactly one process may win; exit codes {exit_codes}"
        assert len([code for code in exit_codes if code != 0]) == self.PROCESSES - 1, exit_codes

        assert _rule_node(rid)["disputed"] is True
        events = _trust_events(rid)
        assert len(events) == 1, f"one approval must produce one TrustEvent, got {events}"
        assert events[0]["kind"] == "dispute"

        audit = _read_stream_rows(project, "audit")
        assert len([r for r in audit if r.get("event") == "rule_disputed"]) == 1
        refusals = [r for r in audit
                    if r.get("event") in {"rule_promotion_claim_lost", "agent_self_approval_blocked"}]
        assert len(refusals) == self.PROCESSES - 1, (
            f"every loser must record its refusal; got {[r.get('event') for r in audit]}"
        )
        assert all(r.get("rule_id") == rid for r in refusals)


# ---------------------------------------------------------------------------
# Capability 36: a token that predates the identity lines still authorizes
# ---------------------------------------------------------------------------


class TestFiveLineTokenStillAuthorizes:
    @pytest.mark.parametrize("action", ["promote", "dispute", "verify"])
    def test_the_action_succeeds_and_the_event_records_empty_identity(
        self, action, graph_rule, tmp_path, monkeypatch,
    ):
        """A hook that predates this change (the installed plugin until release) writes
        five lines. Refusing them would break promotion until the release, so the action
        is authorized and the history says plainly that it does not know who approved."""
        from writ.session.gate_token import gate_token_path

        _env(monkeypatch, tmp_path, f"fiveline-{action}")
        authority = "ai-provisional" if action == "promote" else "human"
        rid = graph_rule(f"fiv{action[:3]}", authority)
        sid = _sid("fiveline")
        with _mint_cleanup(sid):
            binding = _surface(action, rid, sid)
            token = uuid.uuid4().hex
            Path(gate_token_path(sid)).write_text(f"{token}\n\n\n\n{binding}\n")
            result, _notify = _cli(_action_args(action, rid, sid, token))
            assert result.exit_code == 0, result.output

        events = _trust_events(rid)
        assert len(events) == 1, events
        assert events[0]["os_login"] == ""
        assert events[0]["git_name"] == ""


# ---------------------------------------------------------------------------
# Capability 37: --reject is unchanged
# ---------------------------------------------------------------------------


class TestRejectUnchanged:
    """Regression anchors: green before and after this item, pinned so the factoring of the
    promote guard cannot reach reject. Reject stays interactive and ai-provisional-only."""

    def test_a_non_ai_provisional_rule_is_refused(self):
        factory, fake_db = _make_fake_writ_db("human")
        result, _notify = _cli(["review", "ENF-REJECT-901", "--reject"], db_factory=factory)
        assert result.exit_code == 1
        assert "Cannot reject: ENF-REJECT-901 has authority 'human'" in result.output
        fake_db.delete_rule.assert_not_awaited()

    def test_an_ai_provisional_rule_is_deleted_after_confirmation_with_no_trust_record(self):
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with patch("writ.cli._notify_daemon_reload"):
            with patch("writ.cli._writ_db", new=factory):
                result = runner.invoke(app, ["review", "ENF-REJECT-902", "--reject"], input="y\n")
        assert result.exit_code == 0, result.output
        fake_db.delete_rule.assert_awaited_once_with("ENF-REJECT-902")
        fake_db.set_rule_trust_props.assert_not_awaited()
        fake_db.create_trust_event.assert_not_awaited()


# ---------------------------------------------------------------------------
# Capability 38: the candidate route's promotion records its approval
# ---------------------------------------------------------------------------


class TestCandidateRouteRecordsTheApproval:
    CANDIDATE = "TRUSTREV-CANDROUTE-001"

    @pytest.mark.asyncio
    async def test_a_successful_candidate_promotion_records_the_approval_with_the_token_identity(
        self, tmp_path, monkeypatch,
    ):
        import writ.promotion as promotion_module
        import writ.server as server_module
        from tests._graph import connection
        from writ.server import SessionPromoteCandidateRequest
        from writ.server.routes.gate import session_promote_candidate
        from writ.session.gate_token import gate_token_path, mint_gate_token

        _env(monkeypatch, tmp_path, "route-approval")

        async def _stub_promote(*_args, **_kwargs):
            return {"promoted": True, "graduated_via": "test-stub"}

        conn = connection()
        await _delete_rule_and_events(conn, self.CANDIDATE)
        await conn.create_rule(_rule_data(self.CANDIDATE, "ai-provisional"), source_origin="graph-authored")
        monkeypatch.setattr(server_module, "_db", conn)
        monkeypatch.setattr(server_module, "_pipeline", object())
        monkeypatch.setattr(promotion_module, "promote_candidate", _stub_promote)
        sid = _sid("route-approval")
        try:
            with _mint_cleanup(sid):
                token = mint_gate_token(
                    sid, gate="", plan_hash="", candidate_id=self.CANDIDATE,
                    os_login=LOGIN, git_name=GIT_NAME,
                )
                result = await session_promote_candidate(
                    sid, SessionPromoteCandidateRequest(candidate_id=self.CANDIDATE, token=token),
                )
                assert result.get("promoted") is True
                assert not os.path.exists(gate_token_path(sid))

            node = await conn.get_rule(self.CANDIDATE)
            rows = await conn._run(
                "MATCH (e:TrustEvent {rule_id: $rid}) RETURN e", rid=self.CANDIDATE,
            )
            events = [dict(r["e"]) for r in rows]
        finally:
            await _delete_rule_and_events(conn, self.CANDIDATE)
            await conn.close()

        assert node["approval_via"] == "promote_candidate"
        assert node["approved_at"]
        assert len(events) == 1, events
        assert events[0]["kind"] == "approval"
        assert events[0]["via"] == "promote_candidate"
        assert events[0]["session_id"] == sid
        assert events[0]["os_login"] == LOGIN, "identity is read BEFORE the claim deletes the file"
        assert events[0]["git_name"] == GIT_NAME


# ---------------------------------------------------------------------------
# Capability 39: a failed approval record never undoes the promotion
# ---------------------------------------------------------------------------


class TestApprovalRecordFailure:
    def test_the_cli_keeps_the_promotion_prints_one_stderr_line_and_emits_one_exception_row(
        self, tmp_path, monkeypatch,
    ):
        from writ import authoring

        project = _env(monkeypatch, tmp_path, "failure-cli")
        rid = "ENF-FAILREC-001"
        sid = _sid("failure-cli")
        factory, fake_db = _make_fake_writ_db("ai-provisional")
        with _mint_cleanup(sid):
            token = _mint(sid, _surface("promote", rid, sid, db_factory=factory))
            boom = AsyncMock(side_effect=RuntimeError("trust write exploded"))
            with patch.object(authoring, "record_approval", new=boom):
                result, notify = _cli(_action_args("promote", rid, sid, token), db_factory=factory)

        assert result.exit_code == 0, result.output
        fake_db.update_rule_authority.assert_awaited_once_with(rid, "ai-promoted")
        fake_db.update_rule_confidence.assert_awaited_once_with(rid, "peer-reviewed")
        assert "Promoted:" in result.output
        notify.assert_called_once()
        stderr_lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(stderr_lines) == 1, stderr_lines
        assert rid in stderr_lines[0]
        exceptions = [r for r in _read_stream_rows(project, "errors")
                      if r.get("component") == "trust.record_approval"]
        assert len(exceptions) == 1, exceptions
        assert "rule_promoted" in _events(project, "audit"), "the audit row of the promotion stands"

    @pytest.mark.parametrize("action", ["dispute", "verify"])
    def test_a_failed_rule_write_is_the_action_failing_so_it_exits_non_zero(
        self, action, tmp_path, monkeypatch,
    ):
        """Dispute and verify have no such split: their rule write IS the action."""
        _env(monkeypatch, tmp_path, f"failure-{action}")
        rid = "ENF-FAILACT-001"
        sid = _sid("failure-action")
        fake = _TrustFakeDB("human")
        fake.set_rule_trust_props = AsyncMock(side_effect=RuntimeError("graph write exploded"))
        factory, fake_db = _make_fake_writ_db(fake=fake)
        with _mint_cleanup(sid):
            token = _mint(sid, _surface(action, rid, sid, db_factory=factory))
            result, _notify = _cli(_action_args(action, rid, sid, token), db_factory=factory)

        assert result.exit_code != 0
        fake_db.create_trust_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_candidate_route_returns_its_unchanged_response_and_emits_one_exception_row(
        self, tmp_path, monkeypatch,
    ):
        import writ.promotion as promotion_module
        import writ.server as server_module
        from writ import authoring
        from writ.server import SessionPromoteCandidateRequest
        from writ.server.routes.gate import session_promote_candidate
        from writ.session.gate_token import mint_gate_token

        project = _env(monkeypatch, tmp_path, "failure-route")
        stub_result = {"promoted": True, "graduated_via": "test-stub"}

        async def _stub_promote(*_args, **_kwargs):
            return dict(stub_result)

        monkeypatch.setattr(server_module, "_db", object())
        monkeypatch.setattr(server_module, "_pipeline", object())
        monkeypatch.setattr(promotion_module, "promote_candidate", _stub_promote)
        monkeypatch.setattr(
            authoring, "record_approval",
            AsyncMock(side_effect=RuntimeError("trust write exploded")),
        )
        sid = _sid("failure-route")
        with _mint_cleanup(sid):
            token = mint_gate_token(
                sid, gate="", plan_hash="", candidate_id="CAND-FAIL-1",
                os_login=LOGIN, git_name=GIT_NAME,
            )
            result = await session_promote_candidate(
                sid, SessionPromoteCandidateRequest(candidate_id="CAND-FAIL-1", token=token),
            )

        assert result == stub_result, "the response must be exactly what the promotion returned"
        exceptions = [r for r in _read_stream_rows(project, "errors")
                      if r.get("component") == "trust.record_approval"]
        assert len(exceptions) == 1, exceptions


# ---------------------------------------------------------------------------
# Capability 40: the audit stream, and no identity in any row
# ---------------------------------------------------------------------------


class TestAuditStreamMapping:
    def test_dispute_and_verify_events_map_to_the_audit_stream(self):
        from writ.shared.logging import STREAM_MAP, stream_for

        for event in ("rule_disputed", "rule_verified"):
            assert STREAM_MAP[event] == "audit"
            assert stream_for(event) == "audit"

    def test_the_existing_promotion_event_is_still_audit(self):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP["rule_promoted"] == "audit"

    def test_a_refusal_row_carries_no_identity_either(self, tmp_path, monkeypatch):
        from writ.session.gate_token import rule_action_binding

        project = _env(monkeypatch, tmp_path, "norefusal-identity")
        sid = _sid("refusal-identity")
        factory, _fake = _make_fake_writ_db(fake=_TrustFakeDB("human"))
        with _mint_cleanup(sid):
            token = _mint(sid, rule_action_binding("verify", "ENF-LEAK-001"))
            result, _notify = _cli(
                _action_args("dispute", "ENF-LEAK-001", sid, token), db_factory=factory,
            )
        assert result.exit_code != 0
        assert "gate_token_rule_mismatch" in _events(project, "audit")
        _assert_no_identity_in_any_log(project)


# ---------------------------------------------------------------------------
# Capability 41: inspect shows the trust props, and still surfaces only ai-provisional
# ---------------------------------------------------------------------------


class TestInspectShowsTrustProps:
    TRUST_PROPS = {
        "layer": "observed", "basis": "ticket", "deliberate": True,
        "verify_interval_days": 90, "last_verified": "2026-01-02",
        "disputed": True, "superseded": True,
        "approved_at": "2026-03-04T05:06:07+00:00", "approval_via": "review_promote",
    }

    def _inspect(self, tmp_path, monkeypatch, authority: str, **props):
        _env(monkeypatch, tmp_path, "inspect")
        sid = _sid("inspect")
        factory, _fake = _make_fake_writ_db(fake=_TrustFakeDB(authority, **props))
        result, notify = _cli(["review", "ENF-INSPECT-001", "--session-id", sid], db_factory=factory)
        assert result.exit_code == 0, result.output
        notify.assert_not_called()
        return result.output, sid

    def test_every_present_trust_prop_is_printed(self, tmp_path, monkeypatch):
        output, _sid_ = self._inspect(tmp_path, monkeypatch, "human", **self.TRUST_PROPS)
        for value in ("observed", "ticket", "90", "2026-01-02", "2026-03-04T05:06:07", "review_promote"):
            assert value in output, f"{value!r} missing from:\n{output}"
        lowered = output.lower()
        for label in ("layer", "basis", "deliberate", "disputed", "superseded"):
            assert label in lowered, f"{label!r} missing from:\n{output}"

    def test_a_rule_with_no_trust_props_prints_none_of_them(self, tmp_path, monkeypatch):
        output, _sid_ = self._inspect(tmp_path, monkeypatch, "human")
        lowered = output.lower()
        for label in ("layer", "basis", "deliberate", "disputed", "superseded", "approved_at"):
            assert label not in lowered, f"{label!r} printed for a rule that has none:\n{output}"

    def test_an_ai_provisional_rule_is_still_surfaced_for_promotion(self, tmp_path, monkeypatch):
        _output, sid = self._inspect(tmp_path, monkeypatch, "ai-provisional", **self.TRUST_PROPS)
        assert _pending_binding(sid) == "ENF-INSPECT-001"

    def test_any_other_rule_is_inspected_without_a_surfacing(self, tmp_path, monkeypatch):
        _output, sid = self._inspect(tmp_path, monkeypatch, "ai-promoted", **self.TRUST_PROPS)
        assert _pending_binding(sid) == ""
