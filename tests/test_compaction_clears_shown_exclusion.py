"""Program item 1c: compaction makes every shown rule eligible again, in every mode.

Before: cmd_reset_after_compaction emptied only loaded_rule_ids_by_phase[current_phase].
Outside work mode current_phase is None, so the ranked query, the pre-write RAG and the
RAG hooks fell back to the flat loaded_rule_ids, which compaction never touched: a rule
shown before compaction stayed excluded for the rest of the session although the model no
longer had it in context.

After: outside a work phase the exclusion is rule_ids_since_compaction, which compaction
empties. loaded_rule_ids stays the cumulative record, because citation validation,
auto-feedback, the handoff, coverage and the session-end metrics read it as every rule
this session was ever shown.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import writ.server as server
import writ.server.routes.query as qroute
from writ.server.models import PromptBundleRequest
from writ.session.budget_tracking import cmd_update
from writ.session.cache import _read_cache, mutate_cache
from writ.session.config import DEFAULT_SESSION_BUDGET
from writ.session.session_lifecycle import cmd_reset_after_compaction

from tests.fixtures.server_routes import isolated_cache  # noqa: F401
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

LIB_DIR = Path(__file__).resolve().parent.parent / "bin" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
from writ_phase_scoped_rules import phase_scoped_ids  # noqa: E402

SID = "test-1c-compaction"
SHOWN = ["ARCH-ORG-001", "PY-IMPORT-001"]
NON_WORK_MODES = ("conversation", "debug", "review", "investigate", None)


def _seed(**fields) -> None:
    with mutate_cache(SID) as cache:
        cache.update(fields)


def _compact(capsys) -> dict:
    cmd_reset_after_compaction(SID)
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestCompactionClearsTheExclusionOutsideWorkMode:
    @pytest.mark.parametrize("mode", NON_WORK_MODES)
    def test_hook_side_exclusion_is_empty_after_compaction(self, isolated_cache, capsys, mode):
        _seed(mode=mode, current_phase=None, loaded_rule_ids=list(SHOWN))
        _compact(capsys)
        assert phase_scoped_ids(_read_cache(SID)) == []

    @pytest.mark.parametrize("mode", NON_WORK_MODES)
    def test_server_side_exclusion_is_empty_after_compaction(self, isolated_cache, capsys, mode):
        from writ.session.injection_state import retrieval_exclude_ids

        _seed(mode=mode, current_phase=None, loaded_rule_ids=list(SHOWN))
        _compact(capsys)
        assert retrieval_exclude_ids(_read_cache(SID)) == []

    @pytest.mark.asyncio
    async def test_next_ranked_query_excludes_nothing(self, isolated_cache, capsys, monkeypatch):
        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN))
        _compact(capsys)
        monkeypatch.setattr(server, "_pipeline", object())
        fake_query_rules = AsyncMock(return_value={"rules": [], "mode": "standard"})
        monkeypatch.setattr(qroute, "query_rules", fake_query_rules)
        monkeypatch.setattr(qroute, "always_on_bundle",
                            AsyncMock(return_value={"rules": [], "total_tokens": 0}))
        monkeypatch.setattr(server.writ_session, "cmd_update", MagicMock())
        monkeypatch.setattr(server, "_run_cmd_format_locked", lambda payload: "")
        await qroute.prompt_bundle(PromptBundleRequest(session_id=SID, prompt="x"))
        assert fake_query_rules.call_args.args[0].exclude_rule_ids == []

    def test_rules_shown_after_compaction_are_excluded_again(self, isolated_cache, capsys):
        from writ.session.injection_state import retrieval_exclude_ids

        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN))
        _compact(capsys)
        cmd_update(SID, ["--add-rules", json.dumps(["PY-IMPORT-001"])])
        cache = _read_cache(SID)
        assert retrieval_exclude_ids(cache) == ["PY-IMPORT-001"]
        assert phase_scoped_ids(cache) == ["PY-IMPORT-001"]
        assert cache["loaded_rule_ids"] == SHOWN

    def test_a_session_never_compacted_keeps_the_flat_exclusion(self, isolated_cache):
        from writ.session.injection_state import retrieval_exclude_ids

        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN))
        cache = _read_cache(SID)
        assert cache["rule_ids_since_compaction"] is None
        assert retrieval_exclude_ids(cache) == SHOWN
        assert phase_scoped_ids(cache) == SHOWN

    def test_compaction_reports_the_cleared_exclusion(self, isolated_cache, capsys):
        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN))
        result = _compact(capsys)
        assert sorted(result["rules_cleared"]) == SHOWN
        assert result["phase"] is None

    def test_compaction_writes_no_null_phase_bucket(self, isolated_cache, capsys):
        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN),
              loaded_rule_ids_by_phase={})
        _compact(capsys)
        assert _read_cache(SID)["loaded_rule_ids_by_phase"] == {}


class TestWorkModeCompactionUnchanged:
    def test_current_bucket_cleared_other_phases_kept(self, isolated_cache, capsys):
        from writ.session.injection_state import retrieval_exclude_ids

        _seed(mode="work", current_phase="implementation",
              loaded_rule_ids=SHOWN + ["ENF-GATE-001"],
              loaded_rule_ids_by_phase={"implementation": list(SHOWN), "planning": ["ENF-GATE-001"]})
        result = _compact(capsys)
        cache = _read_cache(SID)
        assert cache["loaded_rule_ids_by_phase"] == {"implementation": [], "planning": ["ENF-GATE-001"]}
        assert retrieval_exclude_ids(cache) == []
        assert sorted(result["rules_cleared"]) == SHOWN


class TestCumulativeRecordSurvivesCompaction:
    @pytest.mark.parametrize("mode,phase", [("conversation", None), ("work", "planning")])
    def test_cumulative_fields_untouched(self, isolated_cache, capsys, mode, phase):
        _seed(mode=mode, current_phase=phase, loaded_rule_ids=list(SHOWN),
              always_on_rule_ids=["ENF-PROC-TDD-001"], subagent_rule_ids=["DOC-ARCH-001"])
        _compact(capsys)
        cache = _read_cache(SID)
        assert cache["loaded_rule_ids"] == SHOWN
        assert cache["always_on_rule_ids"] == ["ENF-PROC-TDD-001"]
        assert cache["subagent_rule_ids"] == ["DOC-ARCH-001"]

    def test_epoch_budget_sticky_and_pending_still_reset(self, isolated_cache, capsys):
        _seed(mode="conversation", current_phase=None, loaded_rule_ids=list(SHOWN),
              remaining_budget=100, compaction_epoch=3, last_injected_rule_ids=list(SHOWN))
        _compact(capsys)
        cache = _read_cache(SID)
        assert cache["compaction_epoch"] == 4
        assert cache["remaining_budget"] == DEFAULT_SESSION_BUDGET
        assert cache["last_injected_rule_ids"] == []
        assert cache["post_compact_pending"] is True


PARITY_CACHES = [
    {},
    {"loaded_rule_ids": ["A"]},
    {"loaded_rule_ids": ["A"], "rule_ids_since_compaction": []},
    {"loaded_rule_ids": ["A", "B"], "rule_ids_since_compaction": ["B"]},
    {"loaded_rule_ids_by_phase": {"planning": ["P"]}, "current_phase": "planning",
     "loaded_rule_ids": ["A"], "rule_ids_since_compaction": []},
    {"loaded_rule_ids_by_phase": {}, "current_phase": "planning",
     "loaded_rule_ids": ["A"], "rule_ids_since_compaction": ["A"]},
    {"loaded_rule_ids_by_phase": {"planning": ["P"]}, "current_phase": None,
     "loaded_rule_ids": ["A"]},
]


class TestHookAndServerSelectTheSameExclusion:
    @pytest.mark.parametrize("cache", PARITY_CACHES)
    def test_parity(self, cache):
        from writ.session.injection_state import retrieval_exclude_ids

        assert list(retrieval_exclude_ids(cache)) == list(phase_scoped_ids(cache))
