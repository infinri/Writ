"""Program item 4 (decision recall), workstream B: the decision behind a file's last change,
carried to the model before it writes that file.

Two seams, both on the write path:

  * TestDecisionBlock          /pre-write-check's decision_context (card shape and cap, dedupe
                               per epoch, deny and out-of-root skips, single project
                               resolution, fail-open friction row)
  * TestDispatchHookDecisionContext
                               hooks/scripts/writ-pre-write-dispatch.sh run for real against
                               tests/_stub_daemon.py, with and without the decision_context
                               field
  * TestDispatchHookShape      the hook source keeps its python spawn count, its seven
                               DISPATCH_BLOB lines and its translator anchor

WHAT IS REAL AND WHAT IS NOT. The session cache is a real file under a throwaway
WRIT_CACHE_DIR, read by the real route and written by the real `cmd_update`, so the
per-(path, decision) dedupe and the epoch reset are proven against the record they live in
and not against a mock that returns what it was told to. The database is a duck-typed fake
(the tests/test_gate_write_retrieval_project_scope.py pattern) because the traversal itself
is proven on the isolated graph in tests/test_decision_recall_graph.py; here the fake only
supplies rows and counts calls. The pipeline is absent unless a test needs the RAG block.

Nothing in this module touches a graph or a live daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import writ.server as server
from writ.server import PreWriteCheckRequest, pre_write_check

REPO = Path(__file__).resolve().parent.parent
DISPATCH_HOOK = REPO / "hooks" / "scripts" / "writ-pre-write-dispatch.sh"

PROJECT_ROOT = "/repo/proj-a"
FILE_PATH = "/repo/proj-a/writ/session/recall.py"
REL_PATH = "writ/session/recall.py"
FULL_HASH = "0123456789abcdef0123456789abcdef01234567"


def path_candidates(path: str, root: str) -> list[str]:
    """The one extractor, imported at call time so a missing one fails its own tests."""
    from writ.session.recall import path_candidates as real

    return real(path, root)


def _row(decision_id: str = "D-1", **overrides: Any) -> dict[str, Any]:
    """One get_decisions_for_paths hit, in the shape the read returns."""
    rationale = overrides.pop("rationale", "Gate the write path on the recorded decision. "
                              "A longer explanation follows the first sentence.")
    row = {
        "decision_id": decision_id,
        "title": rationale[:80],
        "rationale": rationale,
        "planned_files": [{"path": REL_PATH, "reason": "planned reason"}],
        "governing_rule_ids": ["RULE-A", "RULE-B"],
        "phase": "planning",
        "ts": "2026-09-01T10:00:00Z",
        "decision_ts": "2026-09-01T10:00:00Z",
        "reason": "wire the decision card into the write gate",
        "change_ts": "2026-09-02T10:00:00Z",
        "commit_hash": FULL_HASH,
        "commit_subject": "Carry the decision to the write gate",
    }
    row.update(overrides)
    return row


class _FakeDecisionDB:
    """resolve_project_for_cwd plus get_decisions_for_paths, counting every call."""

    def __init__(self, rows: dict[str, list[dict]] | None = None, *, project: str = "proj-a",
                 error: Exception | None = None, delay: float = 0.0) -> None:
        self.rows = rows if rows is not None else {REL_PATH: [_row()]}
        self.project = project
        self.error = error
        self.delay = delay
        self.resolve_calls: list[str] = []
        self.read_calls: list[dict[str, Any]] = []
        self.cancelled = False

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        self.resolve_calls.append(cwd)
        return self.project

    async def get_decisions_for_paths(self, project: str, paths: list[str], per_path: int = 1):
        self.read_calls.append({"project": project, "paths": list(paths), "per_path": per_path})
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        return {p: list(self.rows[p]) for p in paths if p in self.rows}

    # Program item 7a/7d: the combined pre-write coroutine also reads open questions and
    # co-change; both return nothing by default so every assertion in this module about the
    # decision card (one friction row, card shape, dedupe, single resolution) holds unchanged.
    async def get_open_questions_for_write(self, project: str, paths: list[str],
                                           rule_ids: list[str], exclude_ids: list[str],
                                           limit: int = 3) -> list[dict]:
        return []

    async def get_cochanged_paths(self, project: str, paths: list[str], **kwargs: Any) -> dict:
        return {}


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(path))
    return path


@pytest.fixture()
def sid(cache_dir) -> str:
    """A session whose real cache file carries the project root and an open write gate."""
    session_id = f"pwdc-{uuid.uuid4().hex[:8]}"
    _seed(session_id, mode="work", project_root=PROJECT_ROOT, remaining_budget=1500,
          current_phase="", compaction_epoch=0)
    return session_id


@pytest.fixture()
def friction(monkeypatch) -> MagicMock:
    spy = MagicMock()
    monkeypatch.setattr(server, "log_friction_event", spy)
    return spy


def _seed(session_id: str, **fields: Any) -> None:
    from writ.session.cache import mutate_cache

    with mutate_cache(session_id) as data:
        data.update(fields)


def _cache(session_id: str) -> dict:
    from writ.session.cache import _read_cache

    return _read_cache(session_id)


def _open_gate(monkeypatch, *, can_write: bool = True, reason: str = "gate closed") -> None:
    monkeypatch.setattr(
        server.writ_session, "_can_write_check",
        lambda session_id, env, skill, cache=None: {"can_write": can_write, "reason": reason},
    )


def _check(session_id: str, file_path: str = FILE_PATH) -> dict[str, Any]:
    req = PreWriteCheckRequest(session_id=session_id, tool_input={"file_path": file_path},
                               file_path=file_path, skill_dir="/x")
    return asyncio.run(pre_write_check(req))


def _install(monkeypatch, db: _FakeDecisionDB | None, *, pipeline=None) -> None:
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_pipeline", pipeline)


def _failure_events(friction: MagicMock) -> list[str]:
    return [c.kwargs.get("event") for c in friction.call_args_list]


# --------------------------------------------------------------------------- #
# /pre-write-check: the decision block
# --------------------------------------------------------------------------- #
class TestDecisionBlock:
    def test_an_allowed_write_to_a_file_with_history_returns_the_decision_card(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        card = out["decision_context"]
        assert out["decision"] == "allow"
        assert isinstance(card, str) and card != ""
        assert "\n" not in card, "the hook flattens rag_rules to one line, so the card is one"
        assert len(card) <= 1000
        assert f"[Writ decision memory: {REL_PATH} last changed under this decision]" in card
        assert "Gate the write path on the recorded decision" in card
        assert "RULE-A" in card and "RULE-B" in card
        assert "wire the decision card into the write gate" in card
        assert "Carry the decision to the write gate" in card
        assert f"({FULL_HASH[:8]})" in card
        assert FULL_HASH[:9] not in card, "the short hash only"

    def test_the_card_title_is_the_first_sentence_not_the_stored_rationale_prefix(
            self, monkeypatch, sid, friction):
        rationale = "Gate the write path. Then explain at length why every single write matters here."
        _install(monkeypatch, _FakeDecisionDB({REL_PATH: [_row(rationale=rationale)]}))
        _open_gate(monkeypatch)
        head = _check(sid)["decision_context"].split(" Why:")[0]
        assert "Gate the write path [RULE-A, RULE-B]" in head
        assert "Then explain" not in head

    def test_a_long_rationale_is_clipped_and_the_whole_card_stays_within_one_thousand_characters(
            self, monkeypatch, sid, friction):
        row = _row(rationale="Because. " + "very long explanation " * 200,
                   reason="r " * 400, commit_subject="s " * 200)
        _install(monkeypatch, _FakeDecisionDB({REL_PATH: [row]}))
        _open_gate(monkeypatch)
        card = _check(sid)["decision_context"]
        assert 0 < len(card) <= 1000
        assert "..." in card
        assert f"[Writ decision memory: {REL_PATH}" in card, "the lead-in survives the clip"

    def test_a_card_with_empty_optional_parts_omits_them(self, monkeypatch, sid, friction):
        row = _row(reason="", commit_subject="", commit_hash="")
        _install(monkeypatch, _FakeDecisionDB({REL_PATH: [row]}))
        _open_gate(monkeypatch)
        card = _check(sid)["decision_context"]
        assert "This file:" not in card
        assert "Commit:" not in card
        assert "Why:" in card

    def test_the_read_asks_for_one_decision_per_path_under_the_resolved_project(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        assert len(db.read_calls) == 1
        call = db.read_calls[0]
        assert call["project"] == "proj-a"
        assert call["per_path"] == 1
        assert call["paths"] == path_candidates(FILE_PATH, PROJECT_ROOT)
        assert REL_PATH in call["paths"]

    def test_the_longest_matched_candidate_path_names_the_card(self, monkeypatch, sid, friction):
        candidates = path_candidates(FILE_PATH, PROJECT_ROOT)
        assert len(candidates) >= 2, "an absolute path under the root expands through ancestors"
        shortest, longest = min(candidates, key=len), max(candidates, key=len)
        db = _FakeDecisionDB({
            shortest: [_row("D-SHORT", rationale="Short match decision.")],
            longest: [_row("D-LONG", rationale="Long match decision.")],
        })
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        card = _check(sid)["decision_context"]
        assert f"[Writ decision memory: {longest} last changed" in card
        assert "Long match decision" in card and "Short match decision" not in card
        assert _cache(sid)["injection_shown"]["pre_write_decision"] == [f"{longest}#D-LONG"]

    def test_the_shown_key_is_recorded_per_path_and_decision_in_the_current_epoch(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, _FakeDecisionDB())
        _open_gate(monkeypatch)
        _check(sid)
        record = _cache(sid)["injection_shown"]
        assert record["epoch"] == "0|"
        assert record["pre_write_decision"] == [f"{REL_PATH}#D-1"]

    def test_a_file_with_no_history_returns_an_empty_context_and_records_nothing(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB({})
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert len(db.read_calls) == 1
        assert "pre_write_decision" not in (_cache(sid).get("injection_shown") or {})
        assert friction.call_count == 0

    def test_the_response_keeps_every_existing_field_beside_the_new_one(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, _FakeDecisionDB())
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["rag_rules"] == ""
        assert out["rag_meta"] == {"rule_ids": [], "tokens": 0}
        assert out["reason"] is None
        assert out["mode"] == "work"
        assert out["max_denial_count"] == 0
        assert {"decision", "reason", "rag_rules", "rag_meta", "mode",
                "max_denial_count", "decision_context"} <= set(out)

    # -- dedupe per (path, decision) per epoch -------------------------------

    def test_a_second_write_to_the_same_file_in_the_same_epoch_returns_an_empty_context(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, _FakeDecisionDB())
        _open_gate(monkeypatch)
        first = _check(sid)["decision_context"]
        second = _check(sid)["decision_context"]
        assert first != ""
        assert second == ""
        assert _cache(sid)["injection_shown"]["pre_write_decision"] == [f"{REL_PATH}#D-1"]

    def test_a_new_commit_linking_the_file_to_a_different_decision_shows_that_decision_once(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        assert _check(sid)["decision_context"] != ""
        db.rows = {REL_PATH: [_row("D-2", rationale="Move the card into the dispatch hook.")]}
        shown = _check(sid)["decision_context"]
        assert "Move the card into the dispatch hook" in shown
        assert _check(sid)["decision_context"] == ""
        assert _cache(sid)["injection_shown"]["pre_write_decision"] == [
            f"{REL_PATH}#D-1", f"{REL_PATH}#D-2"]

    def test_a_different_file_with_the_same_decision_is_a_separate_key(
            self, monkeypatch, sid, friction):
        other_rel = "writ/session/injection_state.py"
        db = _FakeDecisionDB({REL_PATH: [_row()], other_rel: [_row()]})
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        assert _check(sid)["decision_context"] != ""
        other = _check(sid, f"{PROJECT_ROOT}/{other_rel}")["decision_context"]
        assert other_rel in other

    def test_a_compaction_resets_the_dedupe_and_the_decision_shows_again(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, _FakeDecisionDB())
        _open_gate(monkeypatch)
        assert _check(sid)["decision_context"] != ""
        assert _check(sid)["decision_context"] == ""
        _seed(sid, compaction_epoch=1)
        again = _check(sid)["decision_context"]
        assert again != ""
        assert _cache(sid)["injection_shown"] == {
            "epoch": "1|", "pre_write_decision": [f"{REL_PATH}#D-1"]}

    def test_a_phase_change_resets_the_dedupe_and_the_decision_shows_again(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, _FakeDecisionDB())
        _open_gate(monkeypatch)
        assert _check(sid)["decision_context"] != ""
        _seed(sid, current_phase="implementation")
        assert _check(sid)["decision_context"] != ""

    # -- skips ---------------------------------------------------------------

    @pytest.mark.parametrize("denial_counts, expected", [({}, "deny"), ({"gate": 2}, "ask")])
    def test_a_denied_or_ask_write_resolves_nothing_reads_nothing_and_returns_an_empty_context(
            self, monkeypatch, sid, friction, denial_counts, expected):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _seed(sid, denial_counts=denial_counts)
        _open_gate(monkeypatch, can_write=False)
        out = _check(sid)
        assert out["decision"] == expected
        assert out["decision_context"] == ""
        assert db.resolve_calls == []
        assert db.read_calls == []

    def test_a_write_outside_the_session_project_root_performs_no_decision_read(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid, "/elsewhere/other/file.py")
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert db.read_calls == []
        assert friction.call_count == 0

    def test_a_session_without_a_project_root_performs_no_decision_read(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _seed(sid, project_root="")
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision_context"] == ""
        assert db.read_calls == []

    def test_an_unresolvable_project_performs_no_decision_read(self, monkeypatch, sid, friction):
        db = _FakeDecisionDB(project="")
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert db.read_calls == []
        assert friction.call_count == 0

    def test_a_daemon_without_a_database_allows_the_write_with_an_empty_context(
            self, monkeypatch, sid, friction):
        _install(monkeypatch, None)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert friction.call_count == 0

    # -- fail open -----------------------------------------------------------

    def test_a_decision_read_that_raises_allows_the_write_and_logs_one_friction_row(
            self, monkeypatch, sid, friction):
        db = _FakeDecisionDB(error=RuntimeError("graph unavailable"))
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert out["rag_rules"] == ""
        assert out["rag_meta"] == {"rule_ids": [], "tokens": 0}
        assert _failure_events(friction) == ["pre_write_decision_failed"]
        row = friction.call_args.kwargs
        assert row["session_id"] == sid
        assert row["mode"] == "work"
        assert "graph unavailable" in row["error"]
        assert "pre_write_decision" not in (_cache(sid).get("injection_shown") or {})

    def test_a_decision_read_that_exceeds_its_timeout_fails_open_the_same_way(
            self, monkeypatch, sid, friction):
        from writ.server.routes import gate

        assert gate._DECISION_CONTEXT_TIMEOUT_S == 1.0
        monkeypatch.setattr(gate, "_DECISION_CONTEXT_TIMEOUT_S", 0.05)
        db = _FakeDecisionDB(delay=1.0)
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        out = _check(sid)
        assert out["decision"] == "allow"
        assert out["decision_context"] == ""
        assert _failure_events(friction) == ["pre_write_decision_failed"]

    def test_a_decision_read_that_exceeds_its_timeout_is_cancelled_before_the_route_returns(
            self, monkeypatch, sid, friction):
        from writ.server.routes import gate

        monkeypatch.setattr(gate, "_DECISION_CONTEXT_TIMEOUT_S", 0.05)
        db = _FakeDecisionDB(delay=5.0)
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        req = PreWriteCheckRequest(session_id=sid, tool_input={"file_path": FILE_PATH},
                                   file_path=FILE_PATH, skill_dir="/x")

        async def _route_then_settle():
            out = await pre_write_check(req)
            for _ in range(5):
                await asyncio.sleep(0)
            return out, db.cancelled

        out, cancelled = asyncio.run(_route_then_settle())
        assert out["decision"] == "allow"
        assert cancelled, "the timed-out decision read must be cancelled, not left running"
        assert _failure_events(friction) == ["pre_write_decision_failed"]

    def test_a_failing_decision_read_leaves_the_rag_block_untouched(self, monkeypatch, sid, friction):
        pipeline = MagicMock()
        pipeline.query.return_value = {"rules": [], "mode": "standard",
                                       "total_candidates": 0, "latency_ms": 1}
        db = _FakeDecisionDB(error=RuntimeError("boom"))
        _install(monkeypatch, db, pipeline=pipeline)
        _open_gate(monkeypatch)
        out = _check(sid, f"{PROJECT_ROOT}/user_login_handler.py")
        assert out["decision"] == "allow"
        assert pipeline.query.call_count == 1
        assert _failure_events(friction) == ["pre_write_decision_failed"]

    # -- one project resolution ----------------------------------------------

    def test_the_rag_block_and_the_decision_block_resolve_the_project_exactly_once(
            self, monkeypatch, sid, friction):
        pipeline = MagicMock()
        pipeline.query.return_value = {"rules": [], "mode": "standard",
                                       "total_candidates": 0, "latency_ms": 1}
        rel = "user_login_handler.py"
        db = _FakeDecisionDB({rel: [_row()]})
        _install(monkeypatch, db, pipeline=pipeline)
        _open_gate(monkeypatch)
        out = _check(sid, f"{PROJECT_ROOT}/{rel}")
        assert db.resolve_calls == [PROJECT_ROOT], "both blocks share one resolution"
        assert pipeline.query.call_args.kwargs.get("project") == "proj-a"
        assert len(db.read_calls) == 1
        assert out["decision_context"] != ""
        assert out["rag_rules"] == ""

    def test_the_decision_block_alone_resolves_the_project_once(self, monkeypatch, sid, friction):
        db = _FakeDecisionDB()
        _install(monkeypatch, db)
        _open_gate(monkeypatch)
        _check(sid)
        assert db.resolve_calls == [PROJECT_ROOT]


class TestFrictionStream:
    def test_the_failure_event_is_stated_in_the_stream_map_as_friction(self):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP["pre_write_decision_failed"] == "friction"
        assert STREAM_MAP["recall_failed"] == "friction"


# --------------------------------------------------------------------------- #
# The dispatch hook, run for real against the stub daemon
# --------------------------------------------------------------------------- #
from tests._stub_daemon import ALWAYS_ON, PRE_WRITE_CHECK, STUB_HOST, StubDaemon  # noqa: E402

RAG_MARKER = "STUB-RAG-RULE-MARKER"
DECISION_MARKER = "STUB-DECISION-CARD-MARKER"
DECISION_CARD = (f"[Writ decision memory: writ/session/recall.py last changed under this "
                 f"decision] {DECISION_MARKER} [RULE-A]. Why: gate the write path.")
ENVELOPE = json.dumps({
    "session_id": "pwdc-hook-probe",
    "tool_name": "Write",
    "hook_event_name": "PreToolUse",
    "tool_input": {"file_path": "/tmp/pwdc_probe.py", "content": "x = 1\n"},
})
ALLOW = {"decision": "allow", "reason": "", "rag_rules": "",
         "rag_meta": {"rule_ids": [], "tokens": 0}, "mode": "work"}
ALWAYS_ON_EMPTY: dict = {"rules": [], "total_tokens": 0, "cap": 5000}


def _body(**fields: Any) -> dict[str, Any]:
    return {**ALLOW, **fields}


def _run_hook(canned: dict) -> tuple[subprocess.CompletedProcess, StubDaemon]:
    env = os.environ.copy()
    env.pop("WRIT_SOCKET", None)
    env.pop("WRIT_DEBUG", None)
    env["HOME"] = tempfile.mkdtemp(prefix="writ-pwdc-home-")
    env["WRIT_HOST"] = STUB_HOST
    env["WRIT_CACHE_DIR"] = tempfile.mkdtemp(prefix="writ-pwdc-cache-")
    env["WRIT_NO_AUTOSTART"] = "1"
    with StubDaemon(pre_write_check=canned, always_on=ALWAYS_ON_EMPTY) as stub:
        env["WRIT_PORT"] = str(stub.port)
        proc = subprocess.run(["bash", str(DISPATCH_HOOK)], input=ENVELOPE, text=True,
                              capture_output=True, timeout=180, env=env)
    assert stub.saw(*PRE_WRITE_CHECK), f"the hook never reached the stub: {stub.describe()}"
    return proc, stub


def _hso(proc: subprocess.CompletedProcess) -> dict:
    for line in proc.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)["hookSpecificOutput"]
    return {}


class TestDispatchHookDecisionContext:
    def test_a_decision_context_is_emitted_in_the_same_additional_context_after_the_rag_rules(self):
        rag = f"[{RAG_MARKER}] WHEN: a canned trigger. RULE: a canned rule."
        proc, stub = _run_hook(_body(
            rag_rules=rag, rag_meta={"rule_ids": ["STUB-RULE-001"], "tokens": 42},
            decision_context=DECISION_CARD))
        context = _hso(proc).get("additionalContext", "")
        assert RAG_MARKER in context and DECISION_MARKER in context
        assert context.index(RAG_MARKER) < context.index(DECISION_MARKER)
        assert "permissionDecision" not in _hso(proc)
        assert stub.saw(*ALWAYS_ON), "the allow branch still fetches write-time rules"

    def test_a_decision_context_alone_is_still_emitted(self):
        proc, _stub = _run_hook(_body(decision_context=DECISION_CARD))
        context = _hso(proc).get("additionalContext", "")
        assert DECISION_MARKER in context
        assert "permissionDecision" not in _hso(proc)

    def test_a_multi_line_decision_context_is_flattened_to_one_line(self):
        proc, _stub = _run_hook(_body(decision_context=f"first {DECISION_MARKER}\nsecond line"))
        context = _hso(proc).get("additionalContext", "")
        # The translator flattens newlines to U+2028 (LINE SEPARATOR), as it always has.
        assert f"first {DECISION_MARKER}\u2028second line" in context

    def test_a_response_without_the_field_emits_exactly_what_it_emitted_before(self):
        rag = f"[{RAG_MARKER}] WHEN: a canned trigger. RULE: a canned rule."
        base = _body(rag_rules=rag, rag_meta={"rule_ids": ["STUB-RULE-001"], "tokens": 42})
        without, _s1 = _run_hook(base)
        empty, _s2 = _run_hook({**base, "decision_context": ""})
        assert RAG_MARKER in _hso(without).get("additionalContext", "")
        assert DECISION_MARKER not in without.stdout
        assert without.stdout == empty.stdout, "an empty field changes nothing, byte for byte"
        assert without.returncode == empty.returncode == 0

    def test_a_response_without_the_field_and_without_rules_still_emits_nothing(self):
        absent, _s1 = _run_hook(_body())
        empty, _s2 = _run_hook(_body(decision_context=""))
        assert absent.stdout.strip() == "" and empty.stdout.strip() == ""

    def test_a_denial_ignores_a_decision_context_and_keeps_its_reply(self):
        deny = _body(decision="deny", reason="[STUB-GATE] canned denial", max_denial_count=1,
                     decision_context=DECISION_CARD)
        proc, stub = _run_hook(deny)
        hso = _hso(proc)
        assert hso.get("permissionDecision") == "deny"
        assert "[STUB-GATE] canned denial" in hso.get("permissionDecisionReason", "")
        assert DECISION_MARKER not in proc.stdout
        assert not stub.saw(*ALWAYS_ON)


class TestDispatchHookShape:
    """The hook's cost and structure, held where the plan says they are held."""

    SRC = DISPATCH_HOOK.read_text()
    # Measured on the unmodified hook; this item adds no python spawn, so both stay put.
    PYTHON_MENTIONS = 7
    PYTHON_DASH_C_SPAWNS = 4

    def test_the_translator_reads_the_optional_field_with_a_default(self):
        assert "result.get('decision_context', '')" in self.SRC

    def test_no_python_spawn_is_added(self):
        assert self.SRC.count("python3") == self.PYTHON_MENTIONS
        assert len(re.findall(r"python3\s+-c", self.SRC)) == self.PYTHON_DASH_C_SPAWNS

    def test_the_translator_still_writes_seven_lines_and_ends_on_the_mode_anchor(self):
        start = self.SRC.index("DISPATCH_BLOB=$(")
        anchor = "sys.stdout.write(mode + '\\n')\n\" 2>/dev/null || echo \"\")"
        end = self.SRC.index(anchor, start)
        region = self.SRC[start:end + len(anchor)]
        assert region.count("sys.stdout.write(") == 7
        assert region.rstrip().endswith(anchor)

    def test_the_field_is_appended_to_rag_rules_before_the_existing_flattening(self):
        start = self.SRC.index("DISPATCH_BLOB=$(")
        region = self.SRC[start:self.SRC.index("mapfile -t _BLOB_LINES", start)]
        assert region.index("decision_context") < region.index("rag_rules.replace('\\n', '\u2028')")
