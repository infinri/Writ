"""Decision Memory Phase 2 RECALL: tests for compile_recall (writ/session/recall.py).

Every test here is RED until the implementer creates writ/session/recall.py.
Tests fail on ImportError (module missing) or AssertionError, never on a
collection/import error.

CRITICAL isolation guarantee: NO test in this file touches Neo4j. The db
object is a fake Python class with async methods that return canned data.
No Neo4jConnection, no migrate.py, no driver is instantiated.

Run: .venv/bin/python -m pytest tests/test_recall_compile.py

Capability map:
  [compile-1]  compile_recall calls get_rule_statements EXACTLY ONCE over the
               de-duped union of all governing_rule_ids (PERF-BATCH-001)
  [compile-2]  compile_recall drops rationale before any planned_files[].reason
  [compile-3]  compile_recall never drops decision_id, title, governing_rule_ids,
               or rule_statements from a kept decision
  [compile-4]  compile_recall drops a decision WHOLE (and all older decisions)
               when it cannot fit even after evicting all evictable fields
  [compile-5]  compiled payload stays within the token budget
  [compile-6]  briefing stays within ~500-token budget
  [compile-7]  writ/session/recall.py imports no LLM/model client

Program item 4 (decision recall), workstream A, re-bases this file on the card format:
  * _FakeDB gains list_memories and get_decisions_for_paths (and records every read);
  * the batched rule-statement tests run under full=True, the only mode that reads
    statements (a prompt-path briefing never renders one);
  * eviction, protected-field and budget tests assert on the RENDERED briefing cost
    (estimate_tokens of the briefing text), not on a hand-rolled field sum;
  * the briefing content tests follow the card format "- <title> [RULES]".
The ranking, cadence and graph behaviors live in tests/test_decision_recall.py and
tests/test_decision_recall_graph.py.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# Factories (TEST-FIXTURE-001)
# ---------------------------------------------------------------------------

def _decision_factory(
    decision_id: str = "DEC-TEST-001",
    title: str = "Add recall module",
    rationale: str = "Users need read-back of decisions.",
    planned_files: list[dict] | None = None,
    governing_rule_ids: list[str] | None = None,
    phase: str = "planning",
    ts: str = "2026-06-27T10:00:00+00:00",
) -> dict:
    """Minimal well-formed decision dict (already post-processed by get_recent_decisions)."""
    return {
        "decision_id": decision_id,
        "title": title,
        "rationale": rationale,
        "planned_files": planned_files if planned_files is not None else [
            {"path": "writ/session/recall.py", "reason": "add recall logic"}
        ],
        "governing_rule_ids": governing_rule_ids if governing_rule_ids is not None else ["PERF-BATCH-001"],
        "phase": phase,
        "ts": ts,
    }


def _memory_factory(
    name: str = "feedback_testing",
    description: str = "Integration tests hit the real database.",
    type: str = "feedback",
    updated_at: str = "2026-06-27T09:00:00+00:00",
) -> dict:
    """A Memory row exactly as list_memories returns it (no body)."""
    return {
        "name": name,
        "description": description,
        "type": type,
        "status": "live",
        "path": f"memory/{name}.md",
        "links": [],
        "updated_at": updated_at,
    }


def _path_hit(
    decision: dict,
    *,
    reason: str = "changed under this decision",
    change_ts: str = "2026-06-27T10:30:00+00:00",
    commit_hash: str = "abcdef1234567890",
    commit_subject: str = "feat: change the file",
) -> dict:
    """One get_decisions_for_paths row: the decision's own fields plus the change."""
    return {
        "decision_id": decision["decision_id"],
        "title": decision["title"],
        "rationale": decision["rationale"],
        "planned_files": decision["planned_files"],
        "governing_rule_ids": decision["governing_rule_ids"],
        "phase": decision["phase"],
        "decision_ts": decision["ts"],
        "reason": reason,
        "change_ts": change_ts,
        "commit_hash": commit_hash,
        "commit_subject": commit_subject,
    }


class _FakeDB:
    """Fake db for compile_recall tests -- no Neo4j dependency.

    Records each read so tests can assert EXACTLY-ONCE semantics (PERF-BATCH-001)
    and corpus-cache behavior. Returns canned decisions, memories, path hits and
    statements. path_hits maps a normalized path to the rows get_decisions_for_paths
    returns for it; a path absent from the map is unmatched (absent from the result,
    exactly like the real read).
    """

    def __init__(
        self,
        decisions: list[dict] | None = None,
        statements: dict[str, str] | None = None,
        memories: list[dict] | None = None,
        path_hits: dict[str, list[dict]] | None = None,
    ) -> None:
        self._decisions = decisions or []
        self._statements = statements or {}
        self._memories = memories or []
        self._path_hits = path_hits or {}
        self.get_rule_statements_calls: list[list[str]] = []
        self.recent_calls: list[tuple[str, int]] = []
        self.memory_calls: list[tuple[str, bool]] = []
        self.path_calls: list[tuple[str, list[str], int]] = []

    async def get_recent_decisions(self, project: str, limit: int = 20) -> list[dict]:
        self.recent_calls.append((project, limit))
        return list(self._decisions)

    async def list_memories(self, project: str, include_deleted: bool = False) -> list[dict]:
        self.memory_calls.append((project, include_deleted))
        return [dict(m) for m in self._memories]

    async def get_decisions_for_paths(
        self, project: str, paths: list[str], per_path: int = 1
    ) -> dict[str, list[dict]]:
        self.path_calls.append((project, list(paths), per_path))
        return {
            p: [dict(r) for r in self._path_hits[p][:per_path]]
            for p in paths if p in self._path_hits
        }

    async def get_rule_statements(self, rule_ids: list[str]) -> dict[str, str]:
        self.get_rule_statements_calls.append(list(rule_ids))
        return {rid: self._statements.get(rid, f"Statement for {rid}") for rid in rule_ids}


# ---------------------------------------------------------------------------
# Token-cost helper (used in budget tests)
# ---------------------------------------------------------------------------

def _briefing_tokens(result: dict) -> int:
    """The tokens the section budget governs: the rendered briefing text itself."""
    from writ.shared.tokens import estimate_tokens

    return estimate_tokens(result["briefing"], None)


# ---------------------------------------------------------------------------
# Tests: PERF-BATCH-001 (exactly one get_rule_statements call)
# ---------------------------------------------------------------------------

class TestCompileRecallBatchRuleStatements:
    """[compile-1]: get_rule_statements called EXACTLY ONCE over de-duped union.

    Item 4: statements are only read under full=True (the CLI listing that prints them),
    so every batched-statement test runs with full=True and an ample budget.
    """

    @pytest.mark.asyncio
    async def test_single_call_for_two_decisions_with_distinct_rules(self) -> None:
        # [compile-1]: two decisions each with different governing_rule_ids must
        # result in exactly ONE get_rule_statements call covering the union.
        # RED: ImportError (writ/session/recall.py does not exist).
        from writ.session.recall import compile_recall

        decisions = [
            _decision_factory(decision_id="DEC-A", governing_rule_ids=["RULE-001", "RULE-002"]),
            _decision_factory(decision_id="DEC-B", governing_rule_ids=["RULE-003"]),
        ]
        db = _FakeDB(decisions=decisions, statements={
            "RULE-001": "Always validate inputs.",
            "RULE-002": "Batch DB reads.",
            "RULE-003": "Fail open on errors.",
        })

        await compile_recall(db, "writ", full=True, budget=200_000)

        assert len(db.get_rule_statements_calls) == 1, (
            f"get_rule_statements must be called EXACTLY ONCE (PERF-BATCH-001); "
            f"called {len(db.get_rule_statements_calls)} times"
        )

    @pytest.mark.asyncio
    async def test_single_call_covers_deduped_union_of_all_ids(self) -> None:
        # [compile-1]: if two decisions share a rule_id, the batched call must
        # include that id only ONCE (de-duped union).
        # RED: ImportError.
        from writ.session.recall import compile_recall

        decisions = [
            _decision_factory(decision_id="DEC-A", governing_rule_ids=["SHARED-RULE", "RULE-002"]),
            _decision_factory(decision_id="DEC-B", governing_rule_ids=["SHARED-RULE", "RULE-003"]),
        ]
        db = _FakeDB(decisions=decisions)

        await compile_recall(db, "writ", full=True, budget=200_000)

        assert len(db.get_rule_statements_calls) == 1, (
            "get_rule_statements must be called exactly once even with overlapping ids"
        )
        ids_fetched = db.get_rule_statements_calls[0]
        assert ids_fetched.count("SHARED-RULE") == 1, (
            f"SHARED-RULE must appear exactly once in the batched call; "
            f"ids_fetched={ids_fetched!r}"
        )

    @pytest.mark.asyncio
    async def test_single_call_when_no_decisions(self) -> None:
        # [compile-1]: with zero decisions, get_rule_statements must still be
        # called exactly once (with an empty list), never skipped entirely.
        # RED: ImportError.
        from writ.session.recall import compile_recall

        db = _FakeDB(decisions=[])

        await compile_recall(db, "writ", full=True, budget=200_000)

        # Acceptable to call once with [] or to skip the call entirely (both are
        # correct PERF-BATCH-001 implementations -- a zero-id batch is a no-op).
        # The important invariant is it is called AT MOST once.
        assert len(db.get_rule_statements_calls) <= 1, (
            f"get_rule_statements must be called at most once; "
            f"called {len(db.get_rule_statements_calls)} times"
        )

    @pytest.mark.asyncio
    async def test_never_n_plus_one_calls(self) -> None:
        # [compile-1]: with 5 decisions, still exactly one batched call -- never
        # N+1 (one call per decision or one call per rule).
        # RED: ImportError.
        from writ.session.recall import compile_recall

        decisions = [
            _decision_factory(
                decision_id=f"DEC-{i:03d}",
                governing_rule_ids=[f"RULE-{i:03d}"],
            )
            for i in range(5)
        ]
        db = _FakeDB(decisions=decisions)

        await compile_recall(db, "writ", full=True, budget=200_000)

        assert len(db.get_rule_statements_calls) == 1, (
            f"get_rule_statements must never be called N+1 times; "
            f"called {len(db.get_rule_statements_calls)} times for 5 decisions"
        )


# ---------------------------------------------------------------------------
# Tests: eviction order (rationale before the matched-file reasons)
# ---------------------------------------------------------------------------

_PATH_PROMPT = "please update writ/foo.py"
_PROJECT_ROOT = "/repo/proj"
_LONG_RATIONALE = (
    "Because the recall cache must be rebuilt after every harvest, so a stale index "
    "never hides a decision. " * 4
).strip()
_FILE_REASON = "touches the cache writer for harvest"


def _path_db(decision: dict, reason: str = _FILE_REASON, **kwargs: Any) -> _FakeDB:
    """A db whose only decision is reachable through the path read for writ/foo.py."""
    return _FakeDB(
        decisions=[],
        path_hits={"writ/foo.py": [_path_hit(decision, reason=reason)]},
        **kwargs,
    )


async def _recall_path(db: _FakeDB, **kwargs: Any) -> dict:
    from writ.session.recall import compile_recall

    return await compile_recall(
        db, "writ", prompt=_PATH_PROMPT, project_root=_PROJECT_ROOT, **kwargs
    )


class TestCompileRecallEvictionOrder:
    """[compile-2]: rationale dropped before any matched-file reason (card cost)."""

    @pytest.mark.asyncio
    async def test_rationale_dropped_before_matched_file_reason(self) -> None:
        # [compile-2] / cap 12: a budget one token under the full card forces eviction;
        # dropping the rationale alone is enough, so the file's reason must survive.
        decision = _decision_factory(
            decision_id="DEC-EVICT", rationale=_LONG_RATIONALE, governing_rule_ids=["RULE-X"],
        )
        ample = await _recall_path(_path_db(decision), budget=200_000)
        full_tokens = _briefing_tokens(ample)
        assert _FILE_REASON in ample["briefing"], "an ample budget renders the reason"
        assert "why:" in ample["briefing"], "an ample budget renders the rationale line"

        tight = await _recall_path(_path_db(decision), budget=full_tokens - 1)

        kept = tight["decisions"]
        assert [d["decision_id"] for d in kept] == ["DEC-EVICT"]
        assert kept[0]["rationale"] == "", (
            f"rationale must be evicted first; got {kept[0]['rationale']!r}"
        )
        assert kept[0]["matched_files"][0]["reason"] == _FILE_REASON, (
            "the matched file's reason must outlive the rationale"
        )
        assert "why:" not in tight["briefing"]
        assert _FILE_REASON in tight["briefing"]
        assert _briefing_tokens(tight) <= full_tokens - 1

    @pytest.mark.asyncio
    async def test_matched_file_reason_dropped_after_rationale_under_tighter_budget(self) -> None:
        # [compile-2]: under a budget the rationale-less card still exceeds, the
        # matched-file reason goes next; id, title and rule ids never do.
        decision = _decision_factory(
            decision_id="DEC-EVICT2", rationale=_LONG_RATIONALE, governing_rule_ids=["RULE-X"],
        )
        ample = await _recall_path(_path_db(decision), budget=200_000)
        no_rationale = await _recall_path(
            _path_db(decision), budget=_briefing_tokens(ample) - 1
        )
        assert no_rationale["decisions"][0]["rationale"] == ""

        tighter = await _recall_path(
            _path_db(decision), budget=_briefing_tokens(no_rationale) - 1
        )

        kept = tighter["decisions"]
        assert [d["decision_id"] for d in kept] == ["DEC-EVICT2"]
        assert kept[0]["rationale"] == ""
        assert kept[0]["matched_files"][0]["reason"] == ""
        assert _FILE_REASON not in tighter["briefing"]
        assert "RULE-X" in tighter["briefing"], "rule ids are protected"
        assert kept[0]["title"] in tighter["briefing"], "the title is protected"

    @pytest.mark.asyncio
    async def test_both_fields_survive_an_ample_budget(self) -> None:
        # [compile-2]: with an ample budget nothing is evicted; the payload carries the
        # FULL rationale (the card clips it for display only).
        decision = _decision_factory(
            decision_id="DEC-FULL", rationale=_LONG_RATIONALE, governing_rule_ids=["RULE-X"],
        )

        result = await _recall_path(_path_db(decision), budget=200_000)

        kept = result["decisions"]
        assert len(kept) == 1
        assert kept[0]["rationale"] == _LONG_RATIONALE
        assert kept[0]["matched_files"] == [{"path": "writ/foo.py", "reason": _FILE_REASON}]


# ---------------------------------------------------------------------------
# Tests: protected fields never dropped
# ---------------------------------------------------------------------------

class TestCompileRecallProtectedFields:
    """[compile-3]: decision_id, title, governing_rule_ids never dropped; statements
    are read and carried only under full=True."""

    @pytest.mark.asyncio
    async def test_protected_fields_present_on_every_kept_decision(self) -> None:
        # [compile-3]: rationale="" so the stored title is the display title.
        from writ.session.recall import compile_recall

        decisions = [
            _decision_factory(
                decision_id="DEC-PROT-001",
                title="Protected fields test",
                rationale="",
                governing_rule_ids=["ERR-FALLBACK-001"],
            ),
        ]
        db = _FakeDB(
            decisions=decisions,
            statements={"ERR-FALLBACK-001": "All error paths are fail-open."},
        )

        result = await compile_recall(db, "writ", full=True, budget=200_000)

        kept = result["decisions"]
        assert len(kept) == 1, "decision must be kept under ample budget"
        d = kept[0]
        assert d.get("decision_id") == "DEC-PROT-001"
        assert d.get("title") == "Protected fields test"
        assert "ERR-FALLBACK-001" in (d.get("governing_rule_ids") or [])
        assert d["rule_statements"].get("ERR-FALLBACK-001") == "All error paths are fail-open.", (
            f"full mode carries the expanded statement; got {d['rule_statements']!r}"
        )

    @pytest.mark.asyncio
    async def test_without_full_no_statement_is_read_or_rendered_but_the_key_is_stable(self) -> None:
        # [compile-3] / cap 17: the prompt path never renders a statement, so it stops
        # paying for the read; rule_statements stays a stable key holding {}.
        from writ.session.recall import compile_recall

        decision = _decision_factory(
            decision_id="DEC-NOSTMT", rationale="", governing_rule_ids=["ERR-FALLBACK-001"],
        )
        db = _FakeDB(decisions=[decision], statements={"ERR-FALLBACK-001": "STATEMENT-BODY-TEXT"})

        result = await compile_recall(db, "writ")

        assert db.get_rule_statements_calls == []
        assert result["decisions"][0]["rule_statements"] == {}
        assert "STATEMENT-BODY-TEXT" not in result["briefing"]
        assert "ERR-FALLBACK-001" in result["briefing"], "rule ids still render"

    @pytest.mark.asyncio
    async def test_rule_statements_key_present_even_with_empty_rule_ids(self) -> None:
        # [compile-3]: a decision with no governing_rule_ids still has the key ({}).
        from writ.session.recall import compile_recall

        decision = _decision_factory(decision_id="DEC-NORULES", governing_rule_ids=[])
        db = _FakeDB(decisions=[decision])

        result = await compile_recall(db, "writ", full=True, budget=200_000)

        kept = result["decisions"]
        assert len(kept) == 1
        assert kept[0]["rule_statements"] == {}


# ---------------------------------------------------------------------------
# Tests: whole-decision drop when it cannot fit
# ---------------------------------------------------------------------------

class TestCompileRecallWholeDecisionDrop:
    """[compile-4]: an item whose protected fields do not fit is dropped WHOLE and
    nothing ranked below it is kept."""

    @pytest.mark.asyncio
    async def test_decision_dropped_whole_when_protected_fields_exceed_budget(self) -> None:
        # [compile-4]: the protected card head (title plus 60 rule ids, ~150 tokens)
        # exceeds a 50-token budget even with everything evictable gone.
        from writ.session.recall import compile_recall

        decision = _decision_factory(
            decision_id="DEC-HUGE",
            title="Huge rule list",
            rationale="",
            governing_rule_ids=[f"RULE-{i:03d}" for i in range(60)],
        )
        db = _FakeDB(decisions=[decision])

        result = await compile_recall(db, "writ", budget=50)

        assert result["decisions"] == [], (
            f"decision must be dropped WHOLE when its protected fields do not fit; "
            f"got {result['decisions']!r}"
        )
        assert result["cards"] == []

    @pytest.mark.asyncio
    async def test_nothing_ranked_below_an_unfittable_item_is_kept(self) -> None:
        # [compile-4]: the card loop stops after the first item that cannot fit, so a
        # tiny item ranked BELOW the unfittable one is not smuggled in.
        from writ.session.recall import compile_recall

        filler = "A" * 1000  # a ~250-token card head
        decisions = [
            _decision_factory(
                decision_id="DEC-NEWEST", title=filler, rationale="", governing_rule_ids=[],
                planned_files=[], ts="2026-06-27T12:00:00+00:00",
            ),
            _decision_factory(
                decision_id="DEC-OLDER", title=filler, rationale="", governing_rule_ids=[],
                planned_files=[], ts="2026-06-26T10:00:00+00:00",
            ),
            _decision_factory(
                decision_id="DEC-OLDEST", title="tiny", rationale="", governing_rule_ids=[],
                planned_files=[], ts="2026-06-25T08:00:00+00:00",
            ),
        ]
        db = _FakeDB(decisions=decisions)

        result = await compile_recall(db, "writ", budget=400)

        kept_ids = [d["decision_id"] for d in result["decisions"]]
        assert kept_ids == ["DEC-NEWEST"], (
            f"only the newest fits; the older one cannot, and the tiny one ranked "
            f"below it must not be kept; kept={kept_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_result_shape_has_every_key_even_when_empty(self) -> None:
        # [compile-4] shape: briefing, decisions, memories and cards, always present.
        from writ.session.recall import compile_recall

        db = _FakeDB(decisions=[])
        result = await compile_recall(db, "writ", budget=200_000)

        assert set(result) >= {"briefing", "decisions", "memories", "cards"}, list(result)
        assert result["briefing"] == ""
        assert result["decisions"] == [] and result["memories"] == [] and result["cards"] == []


# ---------------------------------------------------------------------------
# Tests: payload within budget
# ---------------------------------------------------------------------------

class TestCompileRecallBudgetBound:
    """[compile-5]: the rendered briefing stays within the token budget."""

    @pytest.mark.asyncio
    async def test_briefing_estimate_never_exceeds_the_budget(self) -> None:
        # [compile-5] / cap 12: 10 medium decisions; whatever the eviction keeps, the
        # estimate of the text that is injected is within the budget. No 10% margin:
        # the cost IS the rendered text.
        from writ.session.recall import compile_recall

        budget = 300
        decisions = [
            _decision_factory(
                decision_id=f"DEC-{i:03d}",
                title="A" * 100,
                rationale=f"Decision number {i} rationale. " + "R" * 200,
                planned_files=[{"path": "writ/f.py", "reason": "Q" * 100}],
                governing_rule_ids=[f"RULE-{i:03d}"],
                ts=f"2026-06-{10 + i:02d}T10:00:00+00:00",
            )
            for i in reversed(range(10))
        ]
        db = _FakeDB(decisions=decisions)

        result = await compile_recall(db, "writ", budget=budget)

        assert result["decisions"], "at least the newest card fits 300 tokens"
        assert _briefing_tokens(result) <= budget, (
            f"briefing estimate {_briefing_tokens(result)} exceeds budget {budget}"
        )
        assert len(result["cards"]) == len(result["decisions"])

    @pytest.mark.asyncio
    async def test_all_decisions_kept_when_budget_is_ample(self) -> None:
        # [compile-5]: with a very large budget all (fewer than 5) decisions are kept.
        from writ.session.recall import compile_recall

        decisions = [_decision_factory(decision_id=f"DEC-{i}") for i in range(3)]
        db = _FakeDB(decisions=decisions)

        result = await compile_recall(db, "writ", budget=200_000)

        assert len(result["decisions"]) == 3, (
            f"all 3 decisions must be kept under ample budget; "
            f"kept={[d['decision_id'] for d in result['decisions']]!r}"
        )


# ---------------------------------------------------------------------------
# Tests: briefing budget
# ---------------------------------------------------------------------------

class TestCompileRecallBriefingBudget:
    """[compile-6]: briefing stays within ~500-token budget."""

    @pytest.mark.asyncio
    async def test_briefing_is_non_empty_when_decisions_kept(self) -> None:
        # [compile-6]: when decisions are kept, the briefing must be a non-empty
        # string (at least a header line).
        # RED: ImportError.
        from writ.session.recall import compile_recall

        db = _FakeDB(decisions=[_decision_factory()])

        result = await compile_recall(db, "writ", budget=200_000)

        briefing = result.get("briefing", "")
        assert isinstance(briefing, str), f"briefing must be a str; got {type(briefing)!r}"
        assert len(briefing) > 0, "briefing must be non-empty when decisions are kept"

    @pytest.mark.asyncio
    async def test_briefing_empty_when_no_decisions_kept(self) -> None:
        # [compile-6]: when no decisions are kept (empty project), the briefing
        # must be an empty string.
        # RED: ImportError.
        from writ.session.recall import compile_recall

        db = _FakeDB(decisions=[])

        result = await compile_recall(db, "writ", budget=200_000)

        assert result.get("briefing") == "", (
            f"briefing must be '' when no decisions exist; got {result.get('briefing')!r}"
        )

    @pytest.mark.asyncio
    async def test_briefing_stays_within_500_token_soft_cap(self) -> None:
        # [compile-6]: the briefing is the once-per-session line block injected
        # via additionalContext. It must stay <= ~500 tokens (2000 chars at 4/token).
        # RED: ImportError.
        from writ.session.recall import compile_recall

        # 20 decisions with long stored titles (rationale "" so the stored title shows)
        # and the DEFAULT budget: PROMPT_SECTION_TOKENS["recall"] is the one budget.
        from writ.shared.tokens import PROMPT_SECTION_TOKENS

        decisions = [
            _decision_factory(
                decision_id=f"DEC-{i:03d}",
                title="A" * 200,  # ~50 tokens per title
                rationale="",
                governing_rule_ids=[f"RULE-{i:03d}", f"RULE-{i:03d}b"],
            )
            for i in range(20)
        ]
        db = _FakeDB(decisions=decisions)

        result = await compile_recall(db, "writ")

        assert _briefing_tokens(result) <= PROMPT_SECTION_TOKENS["recall"], (
            f"briefing must stay within the recall section budget; "
            f"got ~{_briefing_tokens(result)} tokens ({len(result['briefing'])} chars)"
        )
        assert len(result["cards"]) <= 5

    @pytest.mark.asyncio
    async def test_briefing_contains_decision_title(self) -> None:
        # [compile-6]: the briefing must mention the kept decision's title so
        # the agent knows what decisions were made.
        # RED: ImportError.
        from writ.session.recall import compile_recall

        decision = _decision_factory(
            decision_id="DEC-BRIEFING-001",
            title="Add recall route to server",
            rationale="",  # an empty rationale keeps the stored title as the display title
            governing_rule_ids=["ERR-FALLBACK-001"],
        )
        db = _FakeDB(
            decisions=[decision],
            statements={"ERR-FALLBACK-001": "Fail open."},
        )

        result = await compile_recall(db, "writ", budget=200_000)

        briefing = result.get("briefing", "")
        assert "- Add recall route to server [ERR-FALLBACK-001]" in briefing.splitlines(), (
            f"briefing must contain the card head '- <title> [RULES]'; got:\n{briefing!r}"
        )

    @pytest.mark.asyncio
    async def test_briefing_contains_rule_ids(self) -> None:
        # [compile-6]: the briefing must cite the governing rule ids (the
        # rule-grounding is Writ's unique value; it must appear in the header).
        # RED: ImportError.
        from writ.session.recall import compile_recall

        decision = _decision_factory(
            decision_id="DEC-RULES-001",
            title="Some decision",
            governing_rule_ids=["PERF-BATCH-001", "ERR-FALLBACK-001"],
        )
        db = _FakeDB(decisions=[decision])

        result = await compile_recall(db, "writ", budget=200_000)

        briefing = result.get("briefing", "")
        assert "PERF-BATCH-001" in briefing, (
            f"briefing must cite governing rule id PERF-BATCH-001; got:\n{briefing!r}"
        )


# ---------------------------------------------------------------------------
# Tests: no LLM/model client imported
# ---------------------------------------------------------------------------

class TestCompileRecallNoLLMImport:
    """[compile-7]: writ/session/recall.py imports no LLM/model client."""

    def test_recall_module_imports_no_llm_client(self) -> None:
        # [compile-7]: the briefing is built MECHANICALLY (string formatting).
        # Any import of an Anthropic, OpenAI, or similar model client is a
        # violation. We inspect the module source for forbidden import patterns.
        # RED: ImportError (module does not exist yet).
        import importlib.util
        from pathlib import Path

        recall_path = Path(__file__).resolve().parent.parent / "writ" / "session" / "recall.py"
        assert recall_path.exists(), (
            f"writ/session/recall.py must exist at {recall_path}; "
            "if this fails with FileNotFoundError the file has not been created yet"
        )

        source = recall_path.read_text()

        forbidden_patterns = [
            "import anthropic",
            "from anthropic",
            "import openai",
            "from openai",
            "import litellm",
            "from litellm",
            "claude",
            "gpt-",
            "ChatCompletion",
            "Anthropic(",
        ]
        for pattern in forbidden_patterns:
            # claude can appear in comments/docstrings so we check for import-level usage
            if "claude" in pattern.lower():
                # Only flag if it appears as an import token (not in a comment/string)
                import_lines = [
                    ln for ln in source.splitlines()
                    if (pattern in ln) and not ln.strip().startswith("#")
                    and "import" in ln
                ]
                assert not import_lines, (
                    f"recall.py must not import any LLM client; "
                    f"found '{pattern}' in import lines: {import_lines!r}"
                )
            else:
                assert pattern not in source, (
                    f"recall.py must not import any LLM client; "
                    f"found forbidden pattern {pattern!r} in source"
                )

    def test_recall_module_can_be_imported_without_model_side_effects(self) -> None:
        # [compile-7]: importing the module must not trigger any model API call
        # or network access. We import it and assert no network-related global
        # was initialized (check that well-known client attribute names are absent).
        # RED: ImportError.
        import sys
        # Force a fresh import even if cached.
        if "writ.session.recall" in sys.modules:
            del sys.modules["writ.session.recall"]

        from writ.session import recall as recall_mod

        # The module must not have a top-level 'client' or '_client' attribute
        # pointing to a model API client.
        for attr in ("client", "_client", "_llm", "llm", "_model", "model"):
            val = getattr(recall_mod, attr, None)
            if val is not None:
                # Allow None-valued attributes or string constants
                assert isinstance(val, (str, int, float, type(None))), (
                    f"recall module must not have a live model-client attribute '{attr}'; "
                    f"got {val!r}"
                )

    @pytest.mark.asyncio
    async def test_briefing_is_not_empty_string_template(self) -> None:
        # [compile-7]: the mechanical briefing must produce real content (not an
        # empty placeholder that would indicate the implementation calls an LLM
        # and fell back to ""). With a known decision it must contain non-trivial text.
        # RED: ImportError.
        from writ.session.recall import compile_recall

        decision = _decision_factory(
            decision_id="DEC-MECH-001",
            title="Mechanical briefing check",
            governing_rule_ids=["TEST-RULE-001"],
        )
        db = _FakeDB(decisions=[decision], statements={"TEST-RULE-001": "Use mechanical only."})

        result = await compile_recall(db, "writ", budget=200_000)

        briefing = result.get("briefing", "")
        assert len(briefing) > 20, (
            f"briefing must be a substantive mechanical string (>20 chars); "
            f"got {briefing!r}"
        )
